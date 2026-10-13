"""The editor's "Simulate all": the page's step loop over HTTP (/api/sim/start,
/api/sim/auto, /api/sim/apply, /api/sim/load) plays a whole zone, matches the
offline run, needs a log-in and writes nothing."""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CAMPAIGN_ROOT = os.path.join(ROOT, "hex-private", "campaign")
EDITOR = os.path.join(ROOT, "campaign_editor")
sys.path[:0] = [ROOT, CAMPAIGN_ROOT, EDITOR]

LIVE_DB = os.path.join(ROOT, "hconnect.db")
COOKIE = "hex_editor_session"     # auth.COOKIE


def _digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _post(port, path, body, token=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Cookie": f"{COOKIE}={token}"} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _page_loop(port, token, home, race, limit):
    """What app.js autoRun does, step by step."""
    status, started = _post(port, "/api/sim/start", {"zone": home, "race": race}, token)
    assert status == 200, started
    zone, state, memo, last, summary = home, started["state"], None, None, None
    for _ in range(limit + 5):
        status, result = _post(port, "/api/sim/auto", {
            "zone": zone, "state": state, "memo": memo, "home": home, "race": race,
            "limit": limit, "last": last}, token)
        assert status == 200, result
        memo, summary, step = result["memo"], result["summary"], result["step"]
        if "done" in step:
            return summary
        if "enter" in step:
            zone = step["enter"]
            status, loaded = _post(port, "/api/sim/load",
                                   {"zone": zone, "state": step["state"]}, token)
            assert status == 200, loaded
            state, last = loaded["state"], None
        else:
            status, applied = _post(port, "/api/sim/apply",
                                    {"zone": zone, "state": state, "action": step["apply"]},
                                    token)
            assert status == 200, applied
            state, last = applied["state"], {"action": step["apply"], "log": applied["log"]}
    raise AssertionError("the run did not stop")


def test_simulate_all_over_http():
    if not os.path.exists(LIVE_DB):
        print("SKIP: no hconnect.db")
        return
    import auth
    import autoplay
    import catalog
    import server
    tmp = tempfile.mkdtemp(prefix="hex_autoplay_")
    try:
        db_path = os.path.join(tmp, "hconnect.db")
        src, dst = sqlite3.connect(LIVE_DB), sqlite3.connect(db_path)
        src.backup(dst)
        src.close()
        dst.close()
        before = _digest(db_path)
        server.EditorHandler.db_path = server.Path(db_path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.EditorHandler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        token = "autoplay-test-token"
        with auth._LOCK:
            auth._SESSIONS[token] = {"token": token, "user_id": 1, "name": "test",
                                     "admin": False, "seen": time.time()}
        try:
            status, _ = _post(port, "/api/sim/auto", {"zone": "AZ1", "state": {}})
            assert status == 401
            status, bad = _post(port, "/api/sim/auto", {
                "zone": "AZ1", "state": {"flags": []}, "home": "fortromor",
                "race": "Human"}, token)
            assert status == 400 and "overworld" in bad["error"], bad

            summary = _page_loop(port, token, "AZ1", "Human", 2000)
            assert summary["done"] and summary["steps"] <= 2000
            quests = json.load(open(os.path.join(
                CAMPAIGN_ROOT, "campaign_rules", "quests.json")))["quests"]
            done = [q["quest"] for q in summary["quests_completed"]]
            assert done and set(done) <= set(quests), done
            assert summary["tally"] and all(r["count"] > 0 for r in summary["tally"])
            # Every quest finished in the run gave its authored reward.
            sources = {s for r in summary["tally"] for s in r["sources"]}
            assert any(s.startswith("quest ") for s in sources), sources

            # The page's loop and the offline run agree step for step.
            db = catalog.connect(server.Path(db_path))
            try:
                _state, offline = autoplay.run(db, "AZ1", "Human", 2000)
            finally:
                db.close()
            assert offline["steps"] == summary["steps"]
            assert offline["quests_completed"] == summary["quests_completed"]
            assert [(r["kind"], r["guid"], r["count"]) for r in offline["tally"]] == \
                [(r["kind"], r["guid"], r["count"]) for r in summary["tally"]]
        finally:
            httpd.shutdown()
            httpd.server_close()
            with auth._LOCK:
                auth._SESSIONS.pop(token, None)
        assert _digest(db_path) == before, "the run wrote to the database"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
