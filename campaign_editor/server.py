#!/usr/bin/env python3
"""Campaign editor and character-state simulator.

    python3 campaign_editor/server.py [--port 8765] [--db hconnect.db]

Serves the editor page and JSON built from ``hex-private/Records/`` plus a view of the
server database, behind a log-in with the game's accounts (``auth.py``).
Admins edit the shared campaign (``campaign/campaign_data/`` and
``campaign/campaign_rules/``, see ``editing.py``). Other accounts
can inspect and simulate that campaign, load their own characters' saved
states, and save simulator progress back to those characters. It binds to
127.0.0.1 unless ``--host``.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sqlite3
import threading
import traceback
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import auth
import catalog
import editing
from campaign_runtime import catalog_data, overlay

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"


PUBLIC_API = {"/api/me", "/api/login", "/api/logout"}


class EditorHandler(BaseHTTPRequestHandler):
    db_path: Path = catalog.REPO / "hconnect.db"
    session: dict | None = None

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

    def _writable(self):
        """A writable connection for authentication or character progress."""
        return sqlite3.connect(self.db_path, timeout=10)

    def _gate(self, path: str) -> bool:
        """Authenticate; False (after a 401) when the request may not go on."""
        self.session = auth.session_for(self.headers)
        if path.startswith("/api/") and path not in PUBLIC_API and not self.session:
            self._json({"error": "log in first", "login": True}, 401)
            return False
        return True

    def _owners(self) -> dict[int, tuple[int, str]]:
        """``{campaign id: (user id, account name)}``."""
        with closing(self._writable()) as db:
            return {int(cid): (int(uid or 0), name or "")
                    for cid, uid, name in db.execute(
                        "SELECT c.id, COALESCE(ch.user_id, c.user_id), u.name "
                        "FROM campaigns c LEFT JOIN champions ch ON ch.id = c.champion_id "
                        "LEFT JOIN users u ON u.id = COALESCE(ch.user_id, c.user_id)")}

    def _may_see(self, campaign_id: int) -> bool:
        if self.session and self.session["admin"]:
            return True
        owner = self._owners().get(int(campaign_id))
        return bool(owner and self.session and owner[0] == self.session["user_id"])

    def _champion_progress(self, db, campaign_id: int, zone_id: str):
        """``(champion id, campaign part, {zone: part})`` of a campaign row's
        champion when the server runs ``zone_id`` from the rules and that
        champion has rules progress; None otherwise."""
        from campaign_runtime import locate, rules_loader
        row = db.execute("SELECT c.champion_id, c.campaign_type, c.template_name, "
                         "c.state_json, ch.race FROM campaigns c LEFT JOIN champions ch "
                         "ON ch.id = c.champion_id WHERE c.id=?", (int(campaign_id),)).fetchone()
        if not row or not rules_loader.runtime_enabled(zone_id):
            return None
        champ_id, ctype, template, raw, race = row
        try:
            state = json.loads(raw or "{}")
        except ValueError:
            state = {}
        if locate.zone_for_row(ctype, template, state, race) != zone_id:
            return None
        rows = dict(db.execute("SELECT zone, state_json FROM campaign_progress "
                               "WHERE champion_id=?", (champ_id,)).fetchall())
        if "*" not in rows:
            return None
        zones = {z: json.loads(v or "{}") for z, v in rows.items() if z != "*"}
        return champ_id, json.loads(rows["*"] or "{}"), zones

    def _save_to_champion(self, body, loaded):
        """Write an edited simulator state back as the champion's progress."""
        engine = catalog._rules_module()
        zone_id = str(body.get("zone") or "")
        state = body.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("flags"), list):
            raise ValueError("not a simulator state")
        with closing(self._writable()) as db:
            found = self._champion_progress(db, int(body["campaign"]), zone_id)
            if not found:
                raise LookupError("this champion's zone is not run from the rules on "
                                  "the server yet, or they have not played it since "
                                  "it was switched on")
            champ_id = found[0]
            from campaign_runtime import progress, runtime
            with progress.champion_lock(champ_id):
                # Recheck the target after acquiring the same lock used by
                # live campaign requests, then replace progress atomically.
                found = self._champion_progress(db, int(body["campaign"]), zone_id)
                if not found or found[0] != champ_id:
                    raise LookupError("this character no longer has saved progress "
                                      "in this zone")
                campaign_part, zone_part = engine.split_progress(state)
                zone_part["_runtime"] = {}  # no half-finished location or battle
                db.executemany(
                    "INSERT INTO campaign_progress (champion_id, zone, state_json, updated_at) "
                    "VALUES (?, ?, ?, datetime('now')) ON CONFLICT(champion_id, zone) DO UPDATE "
                    "SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                    [(champ_id, "*", json.dumps(campaign_part, sort_keys=True)),
                     (champ_id, zone_id, json.dumps(zone_part, sort_keys=True))])
                # The campaign row and last-campaign pointer follow so the
                # client opens in this zone when the character next loads.
                runtime.activate_zone(db, champ_id, zone_id)
                # The QUEST rows are not part of the zone row: bring them level
                # with the imported quests so the journal matches the engine.
                from campaign_runtime import quests
                journal = quests.reproject(db, champ_id, campaign_part.get("quests") or {})
                db.commit()
        return {"saved": True, "champion": champ_id, "zone": zone_id, "journal": journal}

    def _me(self):
        if not self.session:
            return {"logged_in": False}
        return {"logged_in": True, "name": self.session["name"],
                "admin": self.session["admin"],
                "can_edit_campaign": bool(self.session["admin"]),
                "can_save_character_state": True}

    def _export(self):
        """Export the shared campaign documents (admins only)."""
        docs = {}
        for directory in ("campaign_data", "campaign_rules"):
            for path in sorted((catalog.CAMPAIGN_ROOT / directory).glob("*.json")):
                rel = path.relative_to(catalog.CAMPAIGN_ROOT).as_posix()
                if overlay.is_editable(rel):
                    docs[rel] = json.loads(path.read_text(encoding="utf-8"))
        body = json.dumps({"hex_campaign_export": 1, "kind": "shared",
                           "account": self.session["name"], "docs": docs},
                          indent=1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="hex-campaign-shared.json"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _import(self, body):
        """Replace shared campaign documents (admins only)."""
        docs = body.get("docs") if isinstance(body.get("docs"), dict) else None
        if docs is None:
            raise ValueError("not a campaign export file (no docs)")
        for rel, doc in docs.items():
            if not overlay.is_editable(rel) or not isinstance(doc, dict):
                raise ValueError(f"{rel}: not an editable campaign document")
        engine = catalog._rules_module()
        campaign_data = editing._campaign_data()
        for rel, doc in docs.items():
            if rel.startswith("campaign_data/"):
                problems = campaign_data.validate(doc)
            elif rel.endswith("/quests.json"):
                problems = [] if isinstance(doc.get("quests"), dict) else ["no quests"]
            else:
                problems = [f"{p['where']}: {p['text']}" for p in engine.validate(doc)
                            if p["level"] == "error"]
            if problems:
                raise ValueError(f"{rel}: {'; '.join(map(str, problems[:3]))}")
        with editing._WRITE_LOCK:
            for rel, doc in docs.items():
                editing._write_json(catalog.CAMPAIGN_ROOT / rel, doc)
        return {"imported": len(docs), "into": "shared campaign"}

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        if not self._gate(path):
            return
        self._get(url, path)

    def _get(self, url, path):
        query = parse_qs(url.query)
        zone_id = query.get("zone", [catalog.AREA_TEMPLATE])[0]
        try:
            if path == "/api/me":
                self._json(self._me())
            elif path == "/api/export":
                if not self.session["admin"]:
                    self._json({"error": "campaign export is admin-only"}, 403)
                    return
                self._export()
            elif path in ("/", "/index.html"):
                self._file(STATIC, "index.html")
            elif path.startswith("/static/"):
                self._file(STATIC, path[len("/static/"):])
            elif path.startswith("/generated/"):
                relative = path[len("/generated/"):]
                card = re.fullmatch(r"cards/(a\d{7})\.jpg", relative)
                if card and not (catalog.GENERATED / relative).is_file():
                    # Decoded from the client the first time it is shown.
                    catalog.card_art_on_demand(card.group(1))
                self._file(catalog.GENERATED, relative)
            elif path == "/api/zones":
                self._json(catalog.zones())
            elif path == "/api/area":
                self._json(self._with_db(
                    lambda db: dict(catalog.area_payload(db, zone_id),
                                    player_options=catalog_data.player_options(db))))
            elif path == "/api/campaigns":
                rows = self._with_db(
                    lambda db: catalog.area_campaigns(db, zone_id) if db else [])
                owners = self._owners()
                mine = [dict(r, owner=owners.get(int(r["id"]), (0, ""))[1]) for r in rows
                        if self.session["admin"] or
                        owners.get(int(r["id"]), (0, ""))[0] == self.session["user_id"]]
                self._json(mine)
            elif path == "/api/state":
                campaign_id = int(query.get("id", ["0"])[0])
                if not self._may_see(campaign_id):
                    self._json({"error": "not your character state"}, 403)
                    return
                state = self._with_db(
                    lambda db: catalog.campaign_state(db, campaign_id) if db else None)
                self._json(state or {"error": "no such campaign"},
                           200 if state else 404)
            elif path == "/api/play/status":
                import campaign_playtest
                self._json(campaign_playtest.status(self.session["user_id"]))
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
        if not self._gate(url.path):
            return
        self._post(url)

    def _post(self, url):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > self.MAX_BODY:
                self._json({"error": "request too large"}, 413)
                return
            body = json.loads(self.rfile.read(length) or b"{}")
            zone_id = str(body.get("zone") or catalog.AREA_TEMPLATE)
            if url.path == "/api/login":
                with closing(self._writable()) as db:
                    session = auth.login(db, body.get("name"), body.get("password"))
                if not session:
                    self._json({"error": "unknown account or wrong password"}, 401)
                    return
                payload = json.dumps({"logged_in": True, "name": session["name"],
                                      "admin": session["admin"]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", auth.set_cookie(session["token"]))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if url.path == "/api/logout":
                if self.session:
                    auth.logout(self.session["token"])
                self.send_response(200)
                self.send_header("Set-Cookie", auth.set_cookie("", clear=True))
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            if url.path == "/api/reset":
                self._json({"error": "personal campaign resets are no longer supported"}, 410)
                return
            if (url.path == "/api/import" or url.path.startswith("/api/edit/")) and \
                    not self.session["admin"]:
                self._json({"error": "campaign editing is admin-only"}, 403)
                return
            if url.path == "/api/import":
                self._json(self._import(body))
                return
            if body.get("campaign") and not self._may_see(int(body["campaign"])):
                self._json({"error": "not your character state"}, 403)
                return
            if url.path == "/api/sim/save" and not self.session["admin"]:
                # Writing a simulated state into a champion's real progress is
                # an authoring act: non-admin viewers are read-only.
                self._json({"error": "saving to a champion is admin-only"}, 403)
                return
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
            if url.path == "/api/edit/scene-tags":
                tags = self._with_db(lambda db: editing.save_scene_tags(
                    db, str(body["scene"]), body.get("tags") or []))
                self._json({"tags": tags})
                return
            if url.path == "/api/edit/scene-personality":
                personality = self._with_db(lambda db: editing.save_scene_personality(
                    db, str(body["scene"]), body.get("personality") or None))
                self._json({"personality": personality})
                return
            if url.path == "/api/edit/scene-player":
                # These saves UPDATE the database row too, so they need the
                # writable connection (the request connection is read-only).
                with closing(self._writable()) as db:
                    chosen = editing.save_scene_player(
                        db, str(body["scene"]), body.get("champion") or None,
                        body.get("deck") or None)
                self._json(chosen)
                return
            if url.path == "/api/edit/scene-gameboard":
                with closing(self._writable()) as db:
                    gameboard = editing.save_scene_gameboard(
                        db, str(body["scene"]), body.get("gameboard"))
                self._json({"gameboard": gameboard})
                return
            if url.path == "/api/edit/scene-mods":
                with closing(self._writable()) as db:
                    mods = editing.save_scene_mods(
                        db, str(body["scene"]), body.get("mods") or [])
                self._json({"mods": mods})
                return
            if url.path == "/api/edit/quest-rewards":
                rewards = self._with_db(lambda db: editing.save_quest_rewards(
                    db, str(body["quest"]), body.get("rewards") or {}))
                self._json({"rewards": rewards})
                return
            if url.path == "/api/edit/conversation-override":
                actions = editing.save_conversation_override(
                    zone_id, str(body["conversation"]), str(body.get("ending") or "*"),
                    body.get("actions") or [])
                self._json({"actions": actions})
                return
            if url.path == "/api/edit/offer-order":
                order = editing.save_offer_order(
                    zone_id, str(body["node"]), body.get("order") or [])
                self._json({"order": order})
                return
            if url.path == "/api/edit/node-state":
                tags = editing.save_node_state(
                    zone_id, str(body["node"]), str(body["state"]),
                    body.get("tags"))
                self._json({"tags": tags})
                return
            if url.path == "/api/play":
                # Play: any logged-in account, as itself; writes no campaign
                # data (a playtest battle, see campaign_playtest).
                self._json(self._with_db(lambda db: _create_play(
                    db, self.session, body)))
                return
            if url.path == "/api/sim/auto":
                # Simulate all: the next step of an auto-play run. Read-only:
                # the page applies the step through /api/sim/apply or load.
                import autoplay
                self._json(self._with_db(lambda db: autoplay.api_step(db, body)))
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
                    # A zone the server runs from the rules: the champion's
                    # real progress (it can be saved back); else a
                    # reconstruction from the legacy map state.
                    with closing(self._writable()) as wdb:
                        found = self._champion_progress(wdb, int(body["campaign"]), zone_id)
                    if found:
                        _champ, campaign_part, zones = found
                        state = engine.zone_view(loaded, campaign_part, zones.get(zone_id))
                        self._json({"state": state,
                                    "layout": _sim_layout(engine, loaded, state),
                                    "saveable": True})
                        return
                    state = self._with_db(lambda db: catalog.sim_state_from_campaign(
                        db, int(body["campaign"]), zone_id))
                else:
                    race = body.get("race", "")
                    state = engine.initial_state(
                        loaded, race=race,
                        faction=catalog.FACTION_OF_RACE.get(race, ""))
                self._json({"state": state, "layout": _sim_layout(engine, loaded, state)})
            elif url.path == "/api/sim/load":
                # An exported state file: take it as given, recompute the layout.
                state = body.get("state")
                if not isinstance(state, dict) or not isinstance(
                        state.get("flags"), list):
                    raise ValueError("not a simulator state file")
                self._json({"state": state, "layout": _sim_layout(engine, loaded, state)})
            elif url.path == "/api/sim/save":
                if not body.get("campaign"):
                    raise ValueError("no champion campaign to save to")
                self._json(self._save_to_champion(body, loaded))
            elif url.path == "/api/sim/apply":
                state, log = engine.apply(loaded, body["state"], body["action"])
                self._json({"state": state, "log": log,
                            "layout": _sim_layout(engine, loaded, state)})
            else:
                self._json({"error": "not found"}, 404)
        except (LookupError, ValueError, KeyError) as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def _create_play(db, session, body):
    """Store an editor Play request of ``body["scene"]`` for the logged-in
    account and return the chat command that starts it in the game client
    (``!playtest <token>``); the simulator state (if any) feeds the reward
    ``when`` tags."""
    import campaign_playtest
    scene = str(body.get("scene") or "").strip().lower()
    row = db.execute("SELECT name, title, ai_deck_guid FROM encounter_scenes "
                     "WHERE guid=?", (scene,)).fetchone() if db and scene else None
    if not row:
        raise LookupError(f"unknown encounter scene {scene or '(none)'}")
    if not row[2]:
        raise ValueError(f"{row[1] or row[0]} has no AI deck: not a battle")
    state = body.get("state")
    created = campaign_playtest.create_request(session["user_id"], {
        "scene": scene, "account": session["name"],
        "zone": str(body.get("zone") or ""), "node": str(body.get("node") or ""),
        "sim_state": state if isinstance(state, dict) else None})
    return {"token": created["token"], "command": created["command"],
            "expires_in": campaign_playtest.REQUEST_TTL_SECONDS,
            "scene": scene, "title": row[1] or row[0], "account": session["name"]}


def _sim_layout(engine, loaded, state):
    """The derived layout plus, per node, each offer entry's ``blocked_by``:
    the tags of its ``when`` that do not hold now (empty while it holds)."""
    layout = engine.layout(loaded, state)
    for node_id, info in layout["nodes"].items():
        info["entries"] = engine.explain_entries(loaded, state, node_id)
    return layout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=catalog.REPO / "hconnect.db")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default: this machine only)")
    args = parser.parse_args()
    EditorHandler.db_path = args.db.resolve()
    # Parse the conversation catalogue in the background; the first request
    # that needs it waits on the same lock instead of reparsing.
    threading.Thread(target=catalog.warm_caches, args=(args.db.resolve(),),
                     daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), EditorHandler)
    print(f"Campaign editor: http://{args.host}:{args.port}/  (db: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
