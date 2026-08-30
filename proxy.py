"""Hex TCG HTTP Auth Proxy - handles authentication and news endpoints"""
import hashlib
import http.server
import json
import sys
import time
import os
import urllib.request
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from functools import partial

# Local helpers
import encryption  # hash_password / verify_password
import db as db_layer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
DB_PATH = os.environ.get(
    "HEX_DB_PATH",
    os.path.join(os.path.dirname(__file__), "hconnect.db"),
)
NEWS_DIR = os.path.join(os.path.dirname(__file__), "news")


def news_feed(base_url):
    """Sample Featured Content carousel JSON for the landing page News Events box.

    The client (UINewsEventsViewModel) GETs NewsEventsURL and expects
    {"NewsItemList":[{ImageUrlHD, ImageUrlHDWide, TitleText, ContentText,
    LinkUrl, ShowDuration, ...}]}. Images are loaded via WWW and can be
    either absolute http(s) URLs or local paths under FolderUtils.DataFolder.
    We serve them from the proxy itself so a private server works offline.
    """
    def img(name):
        return base_url + "/news/" + name

    return {
        "NewsItemList": [
            {
                "ImageUrl": img("hex2.png"),
                "ImageUrlHD": img("hex2_newspaper.png"),
                "ImageUrlHDWide": img("hex2_newspaper.png"),
                "TitleText": "",
                "ContentText": "",
                "LinkUrl": "ingame:scene:CardCollection",
                "ShowDuration": 8,
                "UseDetailText": True,
                "DetailTitleText": "",
                "DetailSubTitleText": "",
                "DetailContentText": "",
                "ResizeContentFrame": True,
            },
            {
                "ImageUrl": img("skg_white.png"),
                "ImageUrlHD": img("skg_white.png"),
                "ImageUrlHDWide": img("skg_white.png"),
                "TitleText": "Stop Killing Games",
                "ContentText": "A server shutdown shouldn't mean the end of history.",
                "LinkUrl": "https://en.wikipedia.org/wiki/Stop_Killing_Games",
                "ShowDuration": 8,
                "UseDetailText": True,
                "DetailTitleText": "Stop Killing Games",
                "DetailSubTitleText": "When you own it, you get to keep it",
                "DetailContentText": "This private server exists because shutting games down is wrong.",
                "ResizeContentFrame": True,
            },
            {
                "ImageUrl": img("sculptor.png"),
                "ImageUrlHD": img("sculptor.png"),
                "ImageUrlHDWide": img("sculptor.png"),
                "TitleText": "Release Notes",
                "ContentText": "Private server updates and new features.",
                "LinkUrl": "",
                "ShowDuration": 8,
                "UseDetailText": True,
                "DetailTitleText": "Release Notes",
                "DetailSubTitleText": "Hex TCG Private Server",
                "DetailContentText": (
                    "Frost Ring Arena now supports tiered encounters, elite decks, "
                    "hidden boss information, unique opponents, and run rewards.\n\n"
                    "Card abilities and AI behavior continue to move toward the "
                    "original gamedata definitions, with ongoing PvP and priority "
                    "improvements.\n\n"
                    "Mail, replay, tournament, Docker, and GHCR support are also "
                    "available in the private server build."
                ),
                "ResizeContentFrame": True,
            },
        ]
    }


def _query_str(vals, default=None):
    """Decode a single query-string value from parse_qs output."""
    if not vals or vals[0] is None:
        return default
    v = vals[0]
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def _player_id_from_name(name):
    """Derive a stable numeric player ID from the full identity string.
    Matches db.py:player_id_from_name."""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2 ** 63)


def db_set_user_flags(username, admin=False, mod=False, founder=False, steam_id=None,
                      password=None):
    """Create the user if needed and set their admin/moderator flags.

    Flags are stored as a JSON dict in users.flags so the HConnect server's
    auth:req handler can read them back when the client logs in. The user's
    id is the Steam account ID (the authoritative key) when a steam_id is
    given, otherwise a stable hash of the full identity string
    ("Display#Discriminator"), matching hconnect_server.player_id_from_steam
    / player_id_from_name so both sides agree.
    """
    db = db_layer.connect(DB_PATH)
    if steam_id:
        uid = int(steam_id) % (2 ** 63)
    else:
        uid = None
    row = None
    if uid is not None:
        row = db.execute("SELECT id, flags FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        row = db.execute("SELECT id, flags FROM users WHERE name=?", (username,)).fetchone()
    if row:
        uid = row[0]
        try:
            flags = json.loads(row[1]) if row[1] else {}
        except (ValueError, TypeError):
            flags = {}
    else:
        if uid is None:
            digest = hashlib.md5(username.encode("utf-8")).hexdigest()
            uid = int(digest[:16], 16) % (2 ** 63)
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, gold, platinum, last_login, flags, password_hash) "
            "VALUES (?, ?, 10000, 10000, datetime('now'), '{}', ?)",
            (uid, username, password))
        flags = {}
    # Update password if provided (for register or password change on existing user).
    if password and uid:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (password, uid))
    if admin:
        flags["admin"] = "true"
    else:
        flags.pop("admin", None)
    if mod:
        flags["mod"] = "true"
    else:
        flags.pop("mod", None)
    if founder:
        flags["founder"] = "true"
    else:
        flags.pop("founder", None)
    db.execute("UPDATE users SET flags=? WHERE id=?", (json.dumps(flags), uid))
    db.commit()
    db.close()
    return flags


# Read once at startup; empty → validation skipped (trust steamId param).
_STEAM_API_KEY = os.environ.get("STEAM_WEB_API_KEY", "").strip()
STEAM_APP_ID = "410380"  # Hex TCG Steam AppID


def _validate_steam_ticket(ticket: str, client_sid: str) -> str | None:
    """Call the Steam Web API to validate an encrypted auth ticket.

    Returns the validated SteamID64 on success, or None on failure.
    The *client_sid* is compared against the API response for defence-in-depth;
    the real SteamID from the API is always authoritative.
    """
    url = (f"https://api.steampowered.com/ISteamUserAuth/AuthenticateUserTicket/v1/"
           f"?key={_STEAM_API_KEY}&appid={STEAM_APP_ID}&ticket={ticket}")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  -> STEAM API error: {e}")
        return None
    params = data.get("response", {}).get("params", {})
    result = params.get("result", "")
    if result != "OK":
        print(f"  -> STEAM API rejected ticket: {result}")
        return None
    sid = params.get("steamid", "")
    if not sid:
        return None
    # Warn if client-sent steamId differs from validated ID (benign with
    # non-validated local use, but suspicious for public servers).
    if client_sid and client_sid != sid:
        print(f"  -> STEAM: client claimed {client_sid}, validated as {sid}")
    return sid


# --- Web registration page templates ----------------------------------------

REGISTER_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hex TCG — Create Account</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d0d1a; color: #d4c49a; display: flex;
         justify-content: center; align-items: center; min-height: 100vh; }
  .card { background: #1a1a2e; border: 1px solid #3a2e1e; border-radius: 8px;
          padding: 2rem; width: 100%%; max-width: 400px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }
  h1 { text-align: center; color: #e8c85a; font-size: 1.4rem; margin-bottom: 1.5rem; }
  label { display: block; margin-bottom: 0.3rem; font-size: 0.85rem; color: #a08050; }
  input { width: 100%%; padding: 0.6rem 0.8rem; border: 1px solid #3a2e1e;
          border-radius: 4px; background: #0d0d1a; color: #e8d8a0; font-size: 0.95rem;
          margin-bottom: 1rem; outline: none; }
  input:focus { border-color: #e8c85a; }
  button { width: 100%%; padding: 0.7rem; background: #c4a03a; color: #111;
           border: none; border-radius: 4px; font-size: 1rem; font-weight: 600;
           cursor: pointer; transition: background 0.2s; }
  button:hover { background: #e8c85a; }
  .footer { text-align: center; margin-top: 1rem; font-size: 0.8rem; color: #666; }
</style>
</head>
<body>
<div class="card">
  <h1>Hex TCG Account Registration</h1>
  <form method="post" action="/register">
    <label for="user">Username</label>
    <input id="user" name="userName" type="text" required
           minlength="3" maxlength="24" placeholder="Choose a username" autofocus>
    <label for="pass">Password</label>
    <input id="pass" name="password" type="password" required
           minlength="4" placeholder="At least 4 characters">
    <label for="email">Email (optional)</label>
    <input id="email" name="emailAddress" type="email"
           placeholder="you@example.com">
    <button type="submit">Create Account</button>
  </form>
  <div class="footer">Return to the game and log in with your new account.</div>
</div>
</body>
</html>"""

REGISTER_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Account Created — Hex TCG</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d0d1a; color: #d4c49a; display: flex;
         justify-content: center; align-items: center; min-height: 100vh; }}
  .card {{ background: #1a1a2e; border: 1px solid #2e4a2e; border-radius: 8px;
          padding: 2rem; width: 100%%; max-width: 400px; text-align: center;
          box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
  h1 {{ color: #5ac85a; font-size: 1.4rem; margin-bottom: 1rem; }}
  p {{ margin-bottom: 1.5rem; line-height: 1.5; }}
  strong {{ color: #e8c85a; }}
</style>
</head>
<body>
<div class="card">
  <h1>Account Created!</h1>
  <p>Your account <strong>{username}</strong> is ready.</p>
  <p>Return to the Hex client and log in with your username and password.</p>
  <p style="font-size:0.8rem;color:#666">You can close this browser tab.</p>
</div>
</body>
</html>"""

REGISTER_ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Error — Hex TCG</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d0d1a; color: #d4c49a; display: flex;
         justify-content: center; align-items: center; min-height: 100vh; }}
  .card {{ background: #1a1a2e; border: 1px solid #4a2e2e; border-radius: 8px;
          padding: 2rem; width: 100%%; max-width: 400px; text-align: center;
          box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
  h1 {{ color: #c85a5a; font-size: 1.4rem; margin-bottom: 1rem; }}
  p {{ margin-bottom: 1.5rem; }}
  a {{ color: #e8c85a; }}
</style>
</head>
<body>
<div class="card">
  <h1>Registration Error</h1>
  <p>{error}</p>
  <p><a href="/register">Try again</a></p>
</div>
</body>
</html>"""


class HexAuthProxy(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] {self.client_address[0]} {format % args}")

    def _json(self, data, status=200):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, body=b""):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _serve_html(self, html: str, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            path = self.path.lower()
            parsed = urlparse(self.path)
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{ts}] <<< {self.command} {self.path}", flush=True)

            if "newsevents" in path or path == "/news" or path.startswith("/news?"):
                base = f"http://{self.headers.get('Host', 'localhost')}"
                print(f"  -> NEWS EVENTS ({base})")
                self._json(news_feed(base))

            elif path.startswith("/news/") and path.endswith((".png", ".jpg", ".jpeg")):
                name = os.path.basename(path)
                fpath = os.path.join(NEWS_DIR, name)
                if os.path.exists(fpath):
                    with open(fpath, "rb") as f:
                        body = f.read()
                    ext = os.path.splitext(name)[1].lower()
                    ctype = "image/png" if ext == ".png" else "image/jpeg"
                    print(f"  -> NEWS IMAGE {name} ({len(body)}b)")
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    print(f"  -> NEWS IMAGE MISSING {name}")
                    self._ok(b"")
                return

            elif parsed.path.rstrip("/").lower() in {
                "/accepts.txt", "/collection", "/inventory", "/card", "/deck",
            }:
                print(f"  -> OBSOLETE COLLECTION API: {parsed.path}")
                self._json({"result": "NOT_FOUND"}, status=404)

            elif "steam/login" in path:
                params = parse_qs(parsed.query)

                def _str(vals, default=None):
                    if not vals or vals[0] is None:
                        return default
                    v = vals[0]
                    if isinstance(v, bytes):
                        return v.decode("utf-8", errors="replace")
                    return str(v)

                def _flag(vals):
                    return _str(vals, "0").lower() in ("1", "true", "yes", "on")

                steam_id = _str(params.get("steamId", [None]), "")
                steamauth = _str(params.get("steamauth", [None]), "")

                # Validate the encrypted Steam ticket when an API key is set.
                if _STEAM_API_KEY and steamauth:
                    validated = _validate_steam_ticket(steamauth, steam_id)
                    if not validated:
                        print(f"  -> STEAM LOGIN: ticket validation failed")
                        self._json({"result": "SYS_ERROR", "error_obj": {
                            "UserKey": "STEAM_AUTH_FAILED",
                            "ErrorString": "Steam authentication failed"}})
                        return
                    # Trust the API, not the URL param.
                    steam_id = validated
                    print(f"  -> STEAM LOGIN: ticket validated → {steam_id}")
                elif not _STEAM_API_KEY:
                    print(f"  -> STEAM LOGIN: no API key — trusting steamId={steam_id}")
                else:
                    # API key set but no ticket sent (unusual: Steam not running?)
                    print(f"  -> STEAM LOGIN: API key set but no ticket; "
                          "rejecting (no local fallback)")
                    self._json({"result": "SYS_ERROR", "error_obj": {
                        "UserKey": "NO_STEAM_TICKET",
                        "ErrorString": "Steam must be running"}})
                    return

                # Optional Name=<player> appended to the auth URL lets us
                # choose which player identity this Steam instance logs in as.
                display = _str(params.get("Name", params.get("name", [None])), "TestPlayer")
                disc = _str(params.get("Disc", [None]))
                if not disc:
                    disc = str(int(hashlib.md5(
                        steam_id.encode("utf-8")).hexdigest(), 16) % 10000).zfill(4)
                username = f"{display}#{disc}"

                admin = _flag(params.get("Admin"))
                mod = _flag(params.get("Mod"))
                founder = _flag(params.get("Founder"))
                flags = db_set_user_flags(username, admin=admin, mod=mod,
                                          founder=founder, steam_id=steam_id)
                token = f"steam:{steam_id}" if steam_id else "test_token_abc123"
                print(f"  -> STEAM LOGIN: steamId={steam_id} username={username} flags={flags}")
                self._json({"result": "success", "token": token,
                           "username": username, "pinfo": ""})

            elif "steam/dlccheck" in path:
                print(f"  -> DLC CHECK")
                self._json({"result": "success", "dlcres": {
                    "dlc_info": [], "redeem_dlc": [], "redeem_card": [],
                    "redeem_item": [], "currency": None}})

            elif "steam/transition" in path:
                print(f"  -> STEAM TRANSITION")
                self._json({"result": "success", "token": "test_token_abc123"})

            # Non-Steam CZE login — verify password and return a token.
            elif "auth/hexlogin" in path:
                params = parse_qs(parsed.query)
                user = _query_str(params.get("user", [None]), "TestPlayer")
                password = _query_str(params.get("pass", [None]), "")
                disc = str(int(hashlib.md5(
                    user.encode("utf-8")).hexdigest(), 16) % 10000).zfill(4)
                username = f"{user}#{disc}"
                uid = _player_id_from_name(username)

                # Look up the user record to check password (if set).
                db = db_layer.connect(DB_PATH)
                row = db.execute(
                    "SELECT id, password_hash FROM users WHERE id=?",
                    (uid,)).fetchone()
                if row:
                    stored_hash = row[1]
                    if stored_hash:
                        if not password or not encryption.verify_password(password, stored_hash):
                            db.close()
                            print(f"  -> HEXLOGIN: bad password for {username}")
                            self._json({"result": "INVALID_LOGIN"})
                            return
                    # else: no password set (Steam-created user) — allow
                else:
                    # No existing user — reject (must register first).
                    db.close()
                    print(f"  -> HEXLOGIN: unknown user {username}")
                    self._json({"result": "INVALID_LOGIN"})
                    return
                db.close()

                db_set_user_flags(username, steam_id=str(uid), password=None)
                token = f"steam:{uid}"
                print(f"  -> HEXLOGIN: user={user} username={username} uid={uid}")
                self._json({"result": "success", "token": token,
                           "username": username, "pinfo": ""})

            # Create Account: /auth/hexregister (HexAuth.Register)
            elif "auth/hexregister" in path:
                params = parse_qs(parsed.query)
                user = _query_str(params.get("user", [None]), "TestPlayer")
                password = _query_str(params.get("pass", [None]), "")
                disc = str(int(hashlib.md5(
                    user.encode("utf-8")).hexdigest(), 16) % 10000).zfill(4)
                username = f"{user}#{disc}"
                uid = _player_id_from_name(username)

                db = db_layer.connect(DB_PATH)
                existing = db.execute("SELECT id FROM users WHERE id=?",
                                      (uid,)).fetchone()
                if existing:
                    db.close()
                    print(f"  -> HEXREGISTER: user {username} already exists")
                    self._json({"result": "REG_ERROR"})
                    return
                db.close()

                password_hash = encryption.hash_password(password) if password else None
                db_set_user_flags(username, steam_id=str(uid),
                                  password=password_hash)
                print(f"  -> HEXREGISTER: created {username} uid={uid}")
                self._json({"result": "success"})

            # Other CZE auth endpoints (transition, changepass, totp).
            elif "auth/hextransition" in path or "auth/hextotp" in path:
                params = parse_qs(parsed.query)
                user = _query_str(params.get("user", [None]), "TestPlayer")
                disc = str(int(hashlib.md5(
                    user.encode("utf-8")).hexdigest(), 16) % 10000).zfill(4)
                username = f"{user}#{disc}"
                uid = _player_id_from_name(username)
                token = f"steam:{uid}"
                db_set_user_flags(username, steam_id=str(uid), password=None)
                auth_type = path.split("/")[-1] if "/" in path else "auth"
                print(f"  -> CZE {auth_type}: user={user} username={username} uid={uid}")
                self._json({"result": "success", "token": token,
                           "username": username, "pinfo": ""})

            # Change password: verify old password, store new hash
            elif "auth/hexchangepass" in path:
                params = parse_qs(parsed.query)
                user = _query_str(params.get("user", [None]), "")
                old_pass = _query_str(params.get("pass", [None]), "")
                new_pass = _query_str(params.get("newp", [None]), "")
                disc = str(int(hashlib.md5(
                    user.encode("utf-8")).hexdigest(), 16) % 10000).zfill(4)
                username = f"{user}#{disc}"
                uid = _player_id_from_name(username)

                db = db_layer.connect(DB_PATH)
                row = db.execute(
                    "SELECT id, password_hash FROM users WHERE id=?",
                    (uid,)).fetchone()
                if not row:
                    db.close()
                    print(f"  -> HEXCHANGEPASS: user {username} not found")
                    self._json({"result": "INVALID_LOGIN"})
                    return
                stored_hash = row[1]
                if stored_hash and not encryption.verify_password(old_pass, stored_hash):
                    db.close()
                    print(f"  -> HEXCHANGEPASS: bad old password for {username}")
                    self._json({"result": "INVALID_LOGIN"})
                    return
                if not new_pass or len(new_pass) < 4:
                    db.close()
                    print(f"  -> HEXCHANGEPASS: new password too short for {username}")
                    self._json({"result": "INVALID_REQUEST"})
                    return
                new_hash = encryption.hash_password(new_pass)
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, uid))
                db.commit()
                db.close()
                token = f"steam:{uid}"
                print(f"  -> HEXCHANGEPASS: changed password for {username}")
                self._json({"result": "success", "token": token,
                           "username": username, "pinfo": ""})

            elif "steam" in path or "auth" in path:
                print(f"  -> STEAM/AUTH generic OK")
                self._json({"result": "success", "token": "test_token_abc123"})

            elif path == "/register" or path.startswith("/register?"):
                if self.command == "POST":
                    cl = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(cl) if cl else b""
                    form = parse_qs(raw.decode("utf-8", errors="replace"))
                    _f = lambda k: _query_str(form.get(k, [None]), "")

                    user = _f("userName") or _f("user")
                    password = _f("password")
                    email = _f("emailAddress") or _f("email")

                    if not user or not password:
                        self._serve_html(REGISTER_ERROR_HTML.format(
                            error="Username and password are required."))
                        return
                    if len(user) < 3 or len(user) > 24:
                        self._serve_html(REGISTER_ERROR_HTML.format(
                            error="Username must be 3-24 characters."))
                        return
                    if len(password) < 4:
                        self._serve_html(REGISTER_ERROR_HTML.format(
                            error="Password must be at least 4 characters."))
                        return

                    disc = str(int(hashlib.md5(
                        user.encode("utf-8")).hexdigest(), 16) % 10000).zfill(4)
                    username = f"{user}#{disc}"
                    uid = _player_id_from_name(username)
                    password_hash = encryption.hash_password(password)

                    db = db_layer.connect(DB_PATH)
                    existing = db.execute(
                        "SELECT id FROM users WHERE id=?", (uid,)).fetchone()
                    if existing:
                        db.close()
                        self._serve_html(REGISTER_ERROR_HTML.format(
                            error="This account name is already taken."))
                        return
                    db.execute(
                        "INSERT OR IGNORE INTO users "
                        "(id, name, gold, platinum, last_login, flags, "
                        "password_hash, email, created_at) "
                        "VALUES (?, ?, 10000, 10000, datetime('now'), '{}', "
                        "?, ?, datetime('now'))",
                        (uid, username, password_hash, email or None))
                    db.commit()
                    db.close()
                    print(f"  -> WEB REGISTER: {username} uid={uid} email={email}")
                    self._serve_html(REGISTER_SUCCESS_HTML.format(username=user))
                else:
                    self._serve_html(REGISTER_FORM_HTML)
                return

            elif path.endswith(".txt") or path.endswith(".ini"):
                print(f"  -> EMPTY (file listing)")
                self._ok(b"")

            else:
                print(f"  -> 200 EMPTY")
                self._ok(b"")

        except Exception as e:
            print(f"  -> PROXY ERROR: {e}")
            try: self._ok(b"")
            except: pass

    do_POST = do_GET
    do_HEAD = do_GET


if __name__ == "__main__":
    host = "0.0.0.0"
    print("=" * 60)
    print("  HEX AUTH PROXY")
    print(f"  Listening: {host}:{PORT}")
    print("=" * 60)

    try:
        server = http.server.HTTPServer((host, PORT), HexAuthProxy)
        server.serve_forever()
    except PermissionError:
        print(f"ERROR: Need root for port {PORT}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
    except Exception as e:
        print(f"FATAL: {e}")
