"""Campaign editor Play: the ``!playtest <token>`` chat command, its token
checks (account, one use, expiry), the playtest battle it builds, and that it
changes no saved progress, on a copy of hconnect.db.

Run directly to print the built session (``python3 tests/tests_campaign_playtest.py``).
"""

import hashlib
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CAMPAIGN_ROOT = os.path.join(ROOT, "hex-private", "campaign")
_TMP = tempfile.mkdtemp(prefix="hex_playtest_test_")
_COPY = os.path.join(_TMP, "hconnect.db")
# Copy the database before any server module opens its shared connection.
_src = sqlite3.connect(f"file:{os.path.join(ROOT, 'hconnect.db')}?mode=ro", uri=True)
_dst = sqlite3.connect(_COPY)
_src.backup(_dst)
_src.close()
_dst.close()
os.environ["HEX_DB_PATH"] = _COPY
os.environ["HEX_PLAYTEST_DIR"] = os.path.join(_TMP, "spool")
sys.path[:0] = [ROOT, CAMPAIGN_ROOT]

import db  # noqa: E402
import campaign  # noqa: E402
import campaign_playtest  # noqa: E402
import commands  # noqa: E402

OVERRIDE_SCENE = "6aca3608-0ef6-478b-a089-d76e94b13650"  # Brutecrown Bluff H-1
REWARD_SCENE = "3cf073b0-47fd-4911-953a-d86902890459"    # AZ1 Node 13 Gnash Bridges
SESSION_TABLES = {"game_sessions", "game_cards", "session_events",
                  "session_transactions"}


class FakeHandler:
    """Collects what the server would send to the client."""
    client_reck_id = "7"
    client_uid = 1
    sid = "hcp-test"
    scnt = 0

    def __init__(self, user_id):
        self.user_profile = {"id": user_id, "name": "Playtest"}
        self.sent, self.displayed, self.logged = [], [], []

    def send(self, headers, body=b""):
        self.sent.append((headers, body))

    def push_display_rewards(self, rewards):
        self.displayed.extend(rewards)

    def _log_req(self, text):
        self.logged.append(text)

    def _champion_health_by_guid(self, _guid):
        return 20

    def _champion_starting_health(self, _race, _cls):
        return 20


class FakeSession:
    def __init__(self, camp_id):
        self.session_id = 987654321
        self.session_name = f"camp_{camp_id}"


def _snapshot(conn):
    """A digest of every table except the per-battle session tables."""
    out = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if table in SESSION_TABLES:
            continue
        digest = hashlib.sha1()
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1'):
            digest.update(repr(tuple(row)).encode())
        out[table] = digest.hexdigest()
    return out


def _player():
    row = db._db.execute(
        "SELECT ch.user_id, ch.id, ch.last_deck_id FROM champions ch "
        "JOIN campaigns c ON c.champion_id = ch.id WHERE ch.is_deleted=0 "
        "ORDER BY ch.id DESC LIMIT 1").fetchone()
    return int(row[0]), int(row[1]), row[2]


def _play(user_id, scene):
    handler = FakeHandler(user_id)
    created = campaign_playtest.create_request(user_id, {"scene": scene, "sim_state": None})
    assert created["command"] == f"!playtest {created['token']}"
    reply = commands.handle_command(handler, created["command"], "", "Playtest")
    assert reply.startswith("Playtest: starting"), reply
    active = [campaign_playtest._read(p) for p in
              (campaign_playtest.spool_dir() / "active").glob("*.json")]
    record = next(a for a in active if a["battle"]["scene"] == scene)
    camp_id = record["camp_id"]
    assert camp_id >= campaign_playtest.PLAYTEST_CAMPAIGN_BASE and handler.sent, "gamestarted was not pushed"
    cfg = campaign.resolve_battle_config(handler, db._db, camp_id, f"camp_{camp_id}")
    return handler, record, cfg


def test_play_builds_the_scene_battle_and_saves_nothing():
    user_id, champ_id, deck_id = _player()
    before = _snapshot(db._db)

    # A scene that fixes the player's champion and deck.
    handler, record, cfg = _play(user_id, OVERRIDE_SCENE)
    scene = db._db.execute(
        "SELECT ai_deck_guid, ai_champion_guid, player_champion_guid, "
        "player_deck_guid, gameboard FROM encounter_scenes WHERE guid=?",
        (OVERRIDE_SCENE,)).fetchone()
    assert cfg["scene_guid"] == OVERRIDE_SCENE
    assert cfg["ai_deck_guid"] == scene[0] and cfg["ai_champ_guid"] == scene[1]
    assert cfg["player_champ_guid"] == scene[2]
    assert cfg["player_deck_cards_json"] and len(__import__("json").loads(cfg["player_deck_cards_json"])) > 0
    assert record["battle"]["gameboard"] == scene[4]
    assert not cfg["is_tutorial"] and not cfg["fortune_guid"]
    assert campaign.handle_battle_gameend(handler, db._db, FakeSession(record["camp_id"]),
                                          False, 1)
    print("override scene:", {k: cfg[k] for k in (
        "scene_guid", "ai_deck_guid", "ai_champ_guid", "ai_name", "ai_personality",
        "player_champ_guid", "deck_db_id")}, "gameboard:", record["battle"]["gameboard"])

    # A scene with no player override: the account's own champion and deck.
    handler, record, cfg = _play(user_id, REWARD_SCENE)
    assert record["battle"]["champion_id"] == champ_id
    assert cfg["deck_db_id"] == (deck_id or record["battle"]["deck_id"])
    assert cfg["player_deck_cards_json"] is None
    won = campaign.handle_battle_gameend(handler, db._db, FakeSession(record["camp_id"]),
                                         True, 1)
    assert won
    kinds = sorted((r["type"], r["template"], r["quantity"]) for r in handler.displayed)
    assert ("GOLD", "", 500) in kinds
    assert ("CARD", "c69b5c92-9692-42c9-bfd9-e2d465fc7a26", 1) in kinds
    status = campaign_playtest.status(user_id)
    assert status["state"] == "finished" and status["won"]
    print("reward scene:", {k: cfg[k] for k in ("scene_guid", "ai_deck_guid",
          "player_champ_guid", "deck_db_id")}, "shown:", status["rewards"])

    after = _snapshot(db._db)
    changed = [t for t in before if before[t] != after.get(t)]
    assert not changed, f"Play changed saved tables: {changed}"
    assert not list((campaign_playtest.spool_dir() / "active").glob("*.json"))
    print(f"tables compared: {len(before)}; changed: {changed or 'none'}")


def test_playtest_token_checks():
    user_id, _champ, _deck = _player()
    before = _snapshot(db._db)
    created = campaign_playtest.create_request(user_id, {"scene": REWARD_SCENE})
    token = created["token"]
    assert campaign_playtest.status(user_id)["state"] == "waiting"

    # Another account is refused and the token stays usable for its owner.
    other = FakeHandler(user_id + 1)
    reply = commands.handle_command(other, f"!playtest {token}", "", "Other")
    assert "different account" in reply and not other.sent, reply
    assert "not a playtest token" in commands.handle_command(
        other, "!playtest ../../etc/passwd", "", "Other")

    # The owner uses it once; a second use is refused.
    owner = FakeHandler(user_id)
    assert commands.handle_command(owner, f"!playtest {token}", "", "P").startswith(
        "Playtest: starting")
    reply = commands.handle_command(FakeHandler(user_id), f"!playtest {token}", "", "P")
    assert "already used" in reply, reply
    record = next(campaign_playtest._read(p) for p in
                  (campaign_playtest.spool_dir() / "active").glob("*.json"))
    campaign.handle_battle_gameend(owner, db._db, FakeSession(record["camp_id"]), False, 1)

    # An expired token is refused (and removed).
    stale = campaign_playtest.create_request(user_id, {"scene": REWARD_SCENE})["token"]
    path = campaign_playtest._path("requests", stale)
    campaign_playtest._write(path, dict(campaign_playtest._read(path), created_at=0))
    reply = commands.handle_command(FakeHandler(user_id), f"!playtest {stale}", "", "P")
    assert "expired" in reply and not path.exists(), reply

    after = _snapshot(db._db)
    assert not [t for t in before if before[t] != after.get(t)]
    print("token checks: other account, malformed, reused and expired tokens refused")


if __name__ == "__main__":
    test_play_builds_the_scene_battle_and_saves_nothing()
    test_playtest_token_checks()
    print("PASS test_play_builds_the_scene_battle_and_saves_nothing")
