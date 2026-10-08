#!/usr/bin/env python3
"""Local campaign editor server.

    python3 tools/campaign_editor/server.py [--port 8765] [--db hconnect.db]

Serves the editor page and JSON built from ``Records/`` plus a read-only view
of the server database. It binds to 127.0.0.1 only and never writes to
Records or the database; tag edits are saved to ``campaign_data/`` and
``campaign_rules/`` (see ``editing.py``).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import catalog
import editing

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"


class EditorHandler(BaseHTTPRequestHandler):
    db_path: Path = catalog.REPO / "hconnect.db"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The browser may cancel a request while navigating or refreshing.
            # Its socket is already gone, so just close this request quietly.
            self.close_connection = True

    def _json(self, value, status: int = 200):
        self._send(status, json.dumps(value).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, root: Path, relative: str):
        target = (root / relative).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        content_type = (mimetypes.guess_type(target.name)[0] or
                        "application/octet-stream")
        self._send(200, target.read_bytes(), content_type)

    def _db(self):
        if not self.db_path.exists():
            return None
        return catalog.connect(self.db_path)

    def _with_db(self, fn):
        db = self._db()
        try:
            return fn(db)
        finally:
            if db is not None:
                db.close()

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        query = parse_qs(url.query)
        zone_id = query.get("zone", [catalog.AREA_TEMPLATE])[0]
        try:
            if path in ("/", "/index.html"):
                self._file(STATIC, "index.html")
            elif path.startswith("/static/"):
                self._file(STATIC, path[len("/static/"):])
            elif path.startswith("/generated/"):
                self._file(catalog.GENERATED, path[len("/generated/"):])
            elif path == "/api/zones":
                self._json(catalog.zones())
            elif path == "/api/area":
                self._json(self._with_db(
                    lambda db: catalog.area_payload(db, zone_id)))
            elif path == "/api/campaigns":
                self._json(self._with_db(
                    lambda db: catalog.area_campaigns(db, zone_id) if db else []))
            elif path == "/api/state":
                campaign_id = int(query.get("id", ["0"])[0])
                state = self._with_db(
                    lambda db: catalog.campaign_state(db, campaign_id) if db else None)
                self._json(state or {"error": "no such campaign"},
                           200 if state else 404)
            elif path == "/api/rewards-catalog":
                self._json(self._with_db(lambda db: catalog.rewards_catalog(db)))
            elif path == "/api/rules":
                loaded, problems = self._with_db(
                    lambda db: catalog.rules(db, zone_id))
                self._json({"rules": loaded, "problems": problems})
            elif path.startswith("/api/conversation/"):
                found = catalog.conversation(path.rsplit("/", 1)[-1])
                self._json(found or {"error": "unknown conversation"},
                           200 if found else 404)
            else:
                self._json({"error": "not found"}, 404)
        except LookupError as exc:
            self._json({"error": str(exc)}, 404)
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 503)
        except Exception as exc:  # report, keep serving
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    MAX_BODY = 1 << 20

    def do_POST(self):
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > self.MAX_BODY:
                self._json({"error": "request too large"}, 413)
                return
            body = json.loads(self.rfile.read(length) or b"{}")
            zone_id = str(body.get("zone") or catalog.AREA_TEMPLATE)
            if url.path == "/api/edit/conversation-tags":
                trigger = self._with_db(lambda db: editing.save_conversation_tags(
                    db, str(body["template"]), str(body["conversation"]),
                    body.get("tags") or []))
                self._json({"trigger": trigger})
                return
            if url.path == "/api/edit/quest-tags":
                conditions = self._with_db(lambda db: editing.save_quest_tags(
                    db, str(body["template"]), str(body["quest"]),
                    str(body["conversation"]), body.get("tags") or []))
                self._json({"conditions": conditions})
                return
            if url.path == "/api/edit/conversation-actions":
                trigger = self._with_db(lambda db: editing.save_conversation_actions(
                    db, str(body["template"]), str(body["conversation"]),
                    str(body.get("ending") or "*"), body.get("actions") or []))
                self._json({"trigger": trigger})
                return
            if url.path == "/api/edit/quest-actions":
                conditions = self._with_db(lambda db: editing.save_quest_actions(
                    db, str(body["template"]), str(body["quest"]),
                    str(body["conversation"]), str(body.get("ending") or "*"),
                    body.get("actions") or []))
                self._json({"conditions": conditions})
                return
            if url.path == "/api/edit/scene-rewards":
                rewards = self._with_db(lambda db: editing.save_scene_rewards(
                    db, str(body["scene"]), body.get("rewards") or {}))
                self._json({"rewards": rewards})
                return
            if url.path == "/api/edit/scene-actions":
                actions = self._with_db(lambda db: editing.save_scene_actions(
                    db, str(body["scene"]), body.get("actions") or {}))
                self._json({"actions": actions})
                return
            if url.path == "/api/edit/scene-personality":
                personality = self._with_db(lambda db: editing.save_scene_personality(
                    db, str(body["scene"]), body.get("personality") or None))
                self._json({"personality": personality})
                return
            if url.path == "/api/edit/scene-mods":
                mods = self._with_db(lambda db: editing.save_scene_mods(
                    db, str(body["scene"]), body.get("mods") or []))
                self._json({"mods": mods})
                return
            if url.path == "/api/edit/quest-rewards":
                rewards = self._with_db(lambda db: editing.save_quest_rewards(
                    db, str(body["quest"]), body.get("rewards") or {}))
                self._json({"rewards": rewards})
                return
            if url.path == "/api/edit/node-state":
                tags = editing.save_node_state(
                    zone_id, str(body["node"]), str(body["state"]),
                    body.get("tags"))
                self._json({"tags": tags})
                return
            engine = catalog._rules_module()
            loaded, _problems = self._with_db(lambda db: catalog.rules(db, zone_id))
            if loaded is None:
                self._json({"error": f"no rules file for zone {zone_id}"}, 404)
                return
            if url.path == "/api/sim/start":
                if body.get("carry"):
                    # A teleport: keep champion, flags and quests; arrive at
                    # the node (or the zone's start).
                    carry = body["carry"]
                    state = engine.initial_state(
                        loaded, race=carry.get("race", ""),
                        faction=carry.get("faction", ""))
                    state["flags"] = sorted(set(state["flags"]) |
                                            set(carry.get("flags") or ()))
                    state["quests"] = dict(carry.get("quests") or {})
                    state["keywords"] = engine._keyword_names(
                        engine._keyword_names(state.get("keywords")) +
                        engine._keyword_names(carry.get("keywords")))
                    node = str(body.get("node") or "")
                    if node and node != state["current"]:
                        start = loaded.get("start") or {}
                        state["current"] = node
                        state["nodes"] = {node: {
                            "visits": 1, "failed": False, "retryable": False,
                            # Arriving at the start node finishes it as a
                            # new game would; elsewhere its content waits.
                            "completed": node == start.get("node") and
                            bool(start.get("node_completed", True))}}
                elif body.get("campaign"):
                    state = self._with_db(lambda db: catalog.sim_state_from_campaign(
                        db, int(body["campaign"]), zone_id))
                else:
                    race = body.get("race", "")
                    state = engine.initial_state(
                        loaded, race=race,
                        faction=catalog.FACTION_OF_RACE.get(race, ""))
                self._json({"state": state, "layout": engine.layout(loaded, state)})
            elif url.path == "/api/sim/load":
                # An exported state file: take it as given, recompute the layout.
                state = body.get("state")
                if not isinstance(state, dict) or not isinstance(
                        state.get("flags"), list):
                    raise ValueError("not a simulator state file")
                self._json({"state": state, "layout": engine.layout(loaded, state)})
            elif url.path == "/api/sim/apply":
                state, log = engine.apply(loaded, body["state"], body["action"])
                self._json({"state": state, "log": log,
                            "layout": engine.layout(loaded, state)})
            else:
                self._json({"error": "not found"}, 404)
        except (LookupError, ValueError, KeyError) as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=catalog.REPO / "hconnect.db")
    args = parser.parse_args()
    EditorHandler.db_path = args.db.resolve()
    # Parse the conversation catalogue in the background; the first request
    # that needs it waits on the same lock instead of reparsing.
    threading.Thread(target=catalog.conversations, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), EditorHandler)
    print(f"Campaign editor: http://127.0.0.1:{args.port}/  (db: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
