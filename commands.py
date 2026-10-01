"""Chat/debug commands for the Hex private server.

All commands receive the handler instance (self) for DB access, event sending, etc.
"""
import struct as _struct
import json as _json
import os as _os
import re as _re
import sys as _sys
from pathlib import Path as _Path
from urllib.parse import urlencode as _urlencode

import game_engine
import game_session
import hconnect_server
import encoder
import campaign
from encoder import encode_datawrapper, compress_gzip, encode_sync_event
from db import (get_log_level, latest_session_log_path_for_players,
                player_log_path, session_log_path)


def reload_runtime_modules():
    """Reload loaded Python runtime modules used by the HConnect SIGUSR1 hook.

    Static schema/data initialization and ``hconnect_server`` itself still
    require a full restart.  The accept loop and live handler objects must not
    be recreated while clients are connected.
    """
    import importlib
    import ai as aim
    import gamemodes.tournament_engine as te, gamemodes.tournament_server as ts
    import services.tournament_game as tg, services.chat as sch
    import services.arena as arena_service, services.mail as mail_service
    import encoder as en, db as dbm
    import game_engine as game_engine_module
    import game_session as game_session_module
    import battle_engine as battle_engine_module
    import pvp_db as pvp_db_module
    import profile_db as profile_db_module
    import pve_db as pve_db_module
    import chat_db as chat_db_module
    import replay_db as replay_db_module
    import tournament_db as tournament_db_module
    import application.dispatcher as application_dispatcher
    import application.player_transactions as player_transactions
    import gamedata.play_plan as play_plan
    import rules_port.wire as rules_wire
    import rules_port.runtime_adapter as rules_runtime
    import rules_port.transactions as rules_transactions

    # Reload foundational modules first, then all already-loaded modules in
    # the application/runtime packages.  Filtering sys.modules avoids
    # importing optional services solely because SIGUSR1 was received.
    groups = [
        [dbm],
        [pvp_db_module, profile_db_module, pve_db_module, chat_db_module,
         replay_db_module, tournament_db_module],
        [game_engine_module, game_session_module, battle_engine_module,
         application_dispatcher, player_transactions, play_plan, campaign],
        [rules_transactions, rules_runtime, rules_wire],
        [aim, te, ts, tg, sch, arena_service, mail_service, en],
    ]
    prefixes = ("abilities", "rules_port", "application", "services",
                "gamemodes", "gamedata")
    loaded = {
        module.__name__: module
        for module in _sys.modules.values()
        if module is not None and any(
            module.__name__ == prefix
            or module.__name__.startswith(prefix + ".")
            for prefix in prefixes)
        and module.__name__ not in {"commands", "hconnect_server"}
    }
    for module in sorted(loaded.values(),
                         key=lambda item: item.__name__.count("."),
                         reverse=True):
        groups.append([module])

    reloaded = []
    seen = set()
    for group in groups:
        for module in group:
            if module.__name__ in seen:
                continue
            importlib.reload(module)
            seen.add(module.__name__)
            reloaded.append(module.__name__)
    # When launched as ``python hconnect_server.py``, the live module is
    # ``__main__``. Rebind its tournament globals after reloading.
    hc = _sys.modules.get("__main__")
    if hc is None or not hasattr(hc, "player_handlers"):
        import hconnect_server as hc
    # hconnect_server.py itself is intentionally not reloaded while clients
    # are connected. Rebind profile helpers added to its legacy handler so a
    # SIGUSR1 reload can still expose newly imported DB APIs.
    setattr(hc, "db_get_store_item", profile_db_module.db_get_store_item)
    # HCPHandler inherits ProfileStreamMixin at server import time. Reloading
    # application.profile_stream alone creates a new mixin class, but cannot
    # change methods already copied onto the live handler class. Rebind those
    # methods explicitly so SIGUSR1 fixes profile-stream code without a full
    # socket restart.
    profile_stream_module = importlib.import_module("application.profile_stream")
    handler_cls = getattr(hc, "HCPHandler", None)
    mixin_cls = getattr(profile_stream_module, "ProfileStreamMixin", None)
    rebound = 0
    if handler_cls is not None and mixin_cls is not None:
        for name, value in vars(mixin_cls).items():
            if not name.startswith("__") and callable(value):
                setattr(handler_cls, name, value)
                rebound += 1
    for name, value in {
        "tournament_server": ts,
        "campaign": campaign,
        "player_handlers": te.player_handlers,
        "player_handler_lock": te.player_handler_lock,
        "player_decks": te.player_decks,
        "push_tournament_room_data": te.push_tournament_room_data,
        "build_tournament_desc_json": te.build_tournament_desc_json,
        "build_waiting_room_data": te.build_waiting_room_data,
        "build_tournament_info_data": te.build_tournament_info_data,
        "uid_instance": te.uid_instance,
        "start_waiting_room_game": te.start_waiting_room_game,
        "_encode_enter_tournament_error": te._encode_enter_tournament_error,
        "_make_deck_data": te._make_deck_data,
    }.items():
        setattr(hc, name, value)
    return (f"Reloaded {len(reloaded)} runtime modules + {rebound} "
            "ProfileStream methods + tournament globals rebound: "
            + ", ".join(reloaded))


def _chat_card_link(name, template_guid):
    """Return the client's clickable card-link markup for a template."""
    return (f"[url=OnClick(OnClickLinkedCard::{template_guid}|0|0::"
            f"CardLink_Tooltip);][{name}][/url]")


def _version_command():
    """Return the repository version without requiring the debug console."""
    try:
        return (_Path(__file__).with_name("VERSION").read_text(
            encoding="utf-8").strip())
    except OSError:
        return "Version unavailable"


def _arena_clear_command(handler):
    """Reset the caller's Frost Ring Arena run outside debug mode."""
    from pve_db import db_clear_arena_run
    db_clear_arena_run(handler.user_profile["id"], conn=hconnect_server._db)
    hconnect_server._db.commit()
    return "Arena run cleared"


def _account_cleanup_command(handler):
    from profile_db import db_reset_account
    user_id = int(handler.user_profile["id"])
    db_reset_account(user_id, conn=hconnect_server._db)
    hconnect_server._db.commit()
    # Keep this live handler consistent with the reset profile.
    handler.user_profile.update({
        "gold": 10000, "platinum": 10000, "experience": 0,
        "level": 1, "flags": "{}"})
    return "Account reset (PvE and alt-art cards kept)"


_ERROR_LOG_PATHS = (
    _Path("/tmp/hconnect_log.txt"),
    _Path("/tmp/hconnect_requests.log"),
)
_ERROR_LINE_RE = _re.compile(
    r"(?:\b(?:error|exception|traceback|failed|failure)\b|"
    r"[A-Za-z]+(?:Error|Exception))",
    _re.IGNORECASE,
)
_LOG_TIME_RE = _re.compile(r"^\[(?P<time>\d{2}:\d{2}:\d{2})\]\s*(?P<msg>.*)$")
_ERROR_LOG_TAIL_BYTES = 256 * 1024
_ERROR_MESSAGE_LIMIT = 900
# The issue body can carry the complete bounded report; keep enough context to
# diagnose a multi-step failure without attaching the whole session log.
_SESSION_LOG_TAIL_LINES = 100
_SESSION_LOG_LINE_LIMIT = 360
_SESSION_PENDING_KEYS = (
    "pending_choice", "pending_trigger", "pending_deck_search",
    "pending_conversation", "pending_discard_ability",
    "pending_discard_continuation", "resolution_paused",
)


def _read_log_tail(path):
    """Read the end of a server log without loading an unbounded file."""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, _os.SEEK_END)
            size = stream.tell()
            start = max(0, size - _ERROR_LOG_TAIL_BYTES)
            stream.seek(start)
            data = stream.read()
    except OSError:
        return []

    lines = data.decode("utf-8", errors="replace").splitlines()
    # A tail that starts in the middle of a line cannot be trusted as a
    # complete log entry.  Drop that partial line, but retain all lines when
    # the whole file fit in the bounded read.
    if start:
        lines = lines[1:]
    return lines


def _log_entries(lines):
    """Group timestamped log lines with their continuation lines."""
    entries = []
    current = None
    for line in lines:
        match = _LOG_TIME_RE.match(line)
        if match:
            if current is not None:
                entries.append(current)
            current = [match.group("time"), [match.group("msg").strip()]]
        elif current is not None:
            # ``log_req`` can receive a traceback, so its continuation lines
            # have no timestamp of their own. Keep them attached to the
            # timestamped entry for a useful issue report.
            continuation = line.strip()
            if continuation:
                current[1].append(continuation)
    if current is not None:
        entries.append(current)
    return entries


def _latest_error_from_lines(lines):
    """Return ``(timestamp, message)`` for the newest error in *lines*."""
    for timestamp, message_lines in reversed(_log_entries(lines)):
        message = " ".join(line for line in message_lines if line)
        if message and _ERROR_LINE_RE.search(message):
            return timestamp, message
    return None


def _last_error_from_logs():
    """Return ``(timestamp, message)`` for the newest logged error.

    ``hconnect_log.txt`` contains normal server stdout/stderr, while
    ``hconnect_requests.log`` is the structured request log used by
    ``log_req``.  The former is preferred because it also contains errors
    written through the plain ``log`` helper; the latter keeps the command
    useful if stdout logging was redirected or rotated independently.
    """
    for path in _ERROR_LOG_PATHS:
        error = _latest_error_from_lines(_read_log_tail(path))
        if error is not None:
            return error
    return None


def _chat_safe_error_message(message):
    """Flatten and neutralize log markup before sending it through chat."""
    message = " ".join(str(message).split())
    # ChatManager interprets square brackets as client markup.  An exception
    # should be copy/pasteable text, not a malformed link or formatting tag.
    message = message.replace("[", "(").replace("]", ")")
    if len(message) > _ERROR_MESSAGE_LIMIT:
        message = message[:_ERROR_MESSAGE_LIMIT - 3].rstrip() + "..."
    return message


def _issue_player_tokens(handler):
    """Return stable and protocol player tokens used in session filenames."""
    profile = getattr(handler, "user_profile", None) or {}
    values = [profile.get("id"), getattr(handler, "client_reck_id", None)]
    values.extend(getattr(handler, "_log_session_players", ()) or ())
    tokens = []
    for value in values:
        if value is None:
            continue
        token = str(value).strip()
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _session_log_path_for_issue(session_id=None, player_tokens=()):
    """Find the newest session log for a player, optionally by session ID."""
    if player_tokens:
        path = latest_session_log_path_for_players(
            player_tokens, session_id=session_id)
        if path is not None:
            return path
        # If the active session has not emitted a file yet, still provide the
        # last completed game rather than dropping the game-history section.
        if session_id is not None:
            path = latest_session_log_path_for_players(player_tokens)
            if path is not None:
                return path
    return session_log_path(session_id) if session_id is not None else None


def _player_log_tail(handler):
    """Return the bounded tail of the caller's cross-session log."""
    profile = getattr(handler, "user_profile", None) or {}
    path = player_log_path(profile.get("id"))
    if path is None:
        return []
    return _read_log_tail(path)[-_SESSION_LOG_TAIL_LINES:]


def _session_log_tail(session_id=None, player_tokens=()):
    """Return a bounded tail for the newest matching game-session log."""
    path = _session_log_path_for_issue(session_id, player_tokens)
    if path is None:
        return []
    return _read_log_tail(path)[-_SESSION_LOG_TAIL_LINES:]


def _session_log_display_name(path):
    """Show a session log name without exposing participant IDs in a report."""
    if not path:
        return "selected session log"
    name = _os.path.basename(path)
    if name.startswith("session-"):
        session_token = name[len("session-"):].split("-p", 1)[0]
        return f"session-{session_token}.log"
    return "selected session log"


def _handler_game_session(handler):
    """Find the caller's active session, preferring the persisted DB view."""
    active = getattr(handler, "_active_game_session", None)
    try:
        reck_id = getattr(handler, "client_reck_id", None)
        if reck_id is not None:
            player_uid = encoder.make_uid(
                hconnect_server.UID_TYPE["ServicePlayer"], int(reck_id))
            session = game_session.find_session_by_player(player_uid)
            if session is not None:
                return session
    except Exception:
        pass
    return active


def _display_diagnostic_value(value):
    """Make enum-like state values compact and safe for chat output."""
    if value is None:
        return "-"
    name = getattr(value, "name", None)
    if name:
        return str(name)
    return str(value).replace("ETurnPhases.", "")


def _session_diagnostic_lines(handler, session):
    """Build a small state snapshot useful for diagnosing a remote game."""
    state = getattr(session, "turn_order", {})
    engine = None
    try:
        checkpoint_engine = getattr(handler, "_checkpoint_engine", None)
        if callable(checkpoint_engine):
            engine = checkpoint_engine(session)
            state = engine.load_state(session)
    except Exception:
        # The log tail is still useful when the checkpoint itself is damaged.
        state = getattr(session, "turn_order", {})
    if not isinstance(state, dict):
        state = {}

    phase = state.get("phase")
    if phase is None and engine is not None:
        try:
            phase = engine.current_phase(state)
        except Exception:
            phase = None

    priority = state.get("priority_pid", state.get("priority_player_id"))
    chain_count = len(state.get("stack") or [])
    top_description = "-"
    port = getattr(session, "_rules_port_session", None)
    action_stack = getattr(port, "action_stack", None)
    if action_stack is not None:
        try:
            chain_count = int(action_stack.count)
        except (AttributeError, TypeError, ValueError):
            pass
        priority = getattr(action_stack, "priority_player_id", priority)
        try:
            action = action_stack.peek()
            if action is not None:
                top_description = type(action).__name__
        except Exception:
            pass
    if top_description == "-" and state.get("stack"):
        top = state["stack"][-1]
        if isinstance(top, dict):
            top_description = (str(top.get("kind") or "item") + ":" +
                               str(top.get("ability_guid") or
                                   top.get("source_uid") or "?"))
        else:
            top_description = type(top).__name__

    pending = [key for key in _SESSION_PENDING_KEYS if state.get(key)]
    if getattr(port, "pending_activation", None):
        pending.append("native_activation")
    engine_name = type(engine).__name__ if engine is not None else "unknown"
    return [
        f"Server: version={_version_command()} log_level={get_log_level()}",
        "Session diagnostics: "
        f"id={getattr(session, 'session_id', '-')} "
        f"name={getattr(session, 'session_name', '-') or '-'} "
        f"state={getattr(session, 'state', '-') or '-'}",
        "Game state: "
        f"engine={engine_name} phase={_display_diagnostic_value(phase)} "
        f"phase_idx={state.get('phase_idx', '-')} "
        f"turn={state.get('turn_number', state.get('turn', '-'))} "
        f"turn_player={_display_diagnostic_value(state.get('turn_player', state.get('turn_pid')))} "
        f"priority={_display_diagnostic_value(priority)} "
        f"chain={chain_count} top={top_description} "
        f"pending={','.join(pending) if pending else '-'} "
        f"seed={getattr(session, 'seed_z', '-')}:{getattr(session, 'seed_w', '-')}",
    ]


def _error_report(handler):
    """Return player and latest-game diagnostics for an issue report."""
    session = _handler_game_session(handler)
    player_tokens = _issue_player_tokens(handler)
    session_id = getattr(session, "session_id", None) if session else None
    player_lines = _player_log_tail(handler)
    session_lines = _session_log_tail(session_id, player_tokens)
    if player_lines or session_lines:
        if session is not None:
            report = _session_diagnostic_lines(handler, session)
        else:
            report = [
                f"Server: version={_version_command()} log_level={get_log_level()}",
                "Player diagnostics: authenticated profile",
            ]
        if player_lines:
            report.append("Recent player log:")
            report.extend(player_lines)
        if session_lines:
            session_path = _session_log_path_for_issue(
                session_id, player_tokens)
            report.append(
                "Recent game log: "
                f"{_session_log_display_name(session_path)}")
            report.extend(session_lines)
        safe_lines = []
        for line in report:
            safe = _chat_safe_error_message(line)
            if len(safe) > _SESSION_LOG_LINE_LIMIT:
                safe = safe[:_SESSION_LOG_LINE_LIMIT - 3].rstrip() + "..."
            safe_lines.append(safe)
        return "\n".join(safe_lines)

    error = _last_error_from_logs()
    if error is None:
        return "No server error found in the log"

    timestamp, message = error
    session_suffix = (f" session={session.session_id}"
                      if session is not None else "")
    return (f"Last server error{session_suffix} {timestamp}: "
            f"{_chat_safe_error_message(message)}")


_GITHUB_ISSUE_URL = "HTTPS://github.com/IanUtley/hex-server/issues/new"
# Keep the generated link below common browser/proxy request-line limits. The
# report is still bounded by _error_report; this only truncates unusually long
# tails after the complete URL has been assembled and measured.
_GITHUB_ISSUE_URL_LIMIT = 7500
_GITHUB_ISSUE_TITLE_LIMIT = 256


def _github_issue_url(title, report):
    """Build a prefilled GitHub issue URL with a bounded diagnostics body."""
    body_prefix = "Session diagnostics collected by the Hex server:\n\n```text\n"
    body_suffix = "\n```"
    truncation_note = "\n\n[diagnostics truncated to fit the issue link]"

    def build(report_text):
        # A log line can contain Markdown fences. Neutralize them so the
        # submitted issue keeps the whole report inside its diagnostics block.
        safe_report = str(report_text).replace("```", "``\u200b`")
        body = body_prefix + safe_report + body_suffix
        query = _urlencode({"title": title, "body": body})
        return f"{_GITHUB_ISSUE_URL}?{query}"

    issue_url = build(report)
    if len(issue_url) <= _GITHUB_ISSUE_URL_LIMIT:
        return issue_url

    # Find the largest report prefix that still produces a usable link. This
    # measures the encoded URL, rather than guessing from raw character count.
    low, high = 0, len(str(report))
    report_text = str(report)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = build(report_text[:midpoint].rstrip() + truncation_note)
        if len(candidate) <= _GITHUB_ISSUE_URL_LIMIT:
            low = midpoint
        else:
            high = midpoint - 1
    return build(report_text[:low].rstrip() + truncation_note)


def _issue_command(handler, title):
    """Open a prefilled GitHub issue form for the caller's session."""
    title = str(title or "").strip()
    if not title:
        return "Usage: !issue <title>"
    title = f"[{_version_command()}] {title}"
    if len(title) > _GITHUB_ISSUE_TITLE_LIMIT:
        title = title[:_GITHUB_ISSUE_TITLE_LIMIT - 3].rstrip() + "..."

    issue_url = _github_issue_url(title, _error_report(handler))
    # The fixed label avoids putting user-controlled markup in the chat link.
    return ("Click the link below to open the prefilled GitHub issue; "
            "review the title and diagnostics before submitting: "
            f"[url={issue_url}]Open GitHub issue[/url]")


def _public_help_command():
    return ("Available commands: !help, !commands, !version, !arena-cleanup, "
            "!account-cleanup, !issue <title>")


def _full_help_lines():
    """Return the developer command help without requiring a game session."""
    return [
        "=== Commands ===",
        "!version — show the server version",
        "!arena-cleanup — clear your Frost Ring Arena run",
        "!account-cleanup — reset your account, keeping PvE and alt-art cards",
        "!issue <title> — open a prefilled GitHub issue with session diagnostics",
        "!game_end victory|defeat — end the campaign battle (test win/loss)",
        "!encounter <name> — start a named campaign encounter",
        "!challenge [opponent] — create a duel challenge",
        "!reload — reload runtime modules",
        "!hand — list cards in hand (name [id])",
        "!aihand — reveal the AI hand",
        "!playable [id|name ...] — set golden outlines (no args = all)",
        "!gencard <name> — generate a copy of a card template to your hand",
        "!addcard <name|id> — draw the next copy of that card from your deck",
        "!threshold[/thresholds] [me|opp] C B R S W D — set 6 threshold counts",
        "!resource [/resource] [me|opp] <current> <maximum> — set resources",
        "!charge [me|opp] <N> — set champion charges",
        "!spellpoints [me|opp] <N> — set champion spell points",
        "!health [me|opp] <N> — set champion health",
        "!pass — advance turn phase",
        "!phase <Name> — jump to phase",
        "!draw N — draw N cards",
        "!discard — discard a random card from your hand",
        "!top <id|name> — put a card from your hand on top of your deck",
        "!zones — list cards by zone",
        "!move <id> <zone> — move card to zone",
        "!state <id> <flags> — set card state (Tapped|Attacking|...)",
        "!attr/!attributes <id> <flags> — set card attributes (Flight|Speed|...)",
        "!update <id> — resend CardUpdated for a card",
        "!help / !commands — this list",
    ]


def handle_command(handler, cmd: str, room: str, username: str) -> str:
    parts = cmd.strip().split()
    action = parts[0].lower().lstrip("!/") if parts else ""
    if action == "version":
        return _version_command()
    if action == "arena-cleanup":
        try:
            return _arena_clear_command(handler)
        except Exception as exc:
            return f"Error: {exc}"
    if action == "account-cleanup":
        try:
            return _account_cleanup_command(handler)
        except Exception as exc:
            return f"Error: {exc}"
    if action == "issue":
        raw_command = cmd.strip()
        title = raw_command[len(parts[0]):].strip() if parts else ""
        return _issue_command(handler, title)
    if action in ("help", "commands") and "allowcon" not in getattr(
            hconnect_server, "PROFILE_FEATURE_FLAGS", ()):
        return _public_help_command()
    # Help is informational and must remain available from chat while the
    # client is on the panorama.  It should not fall through to the active
    # game/session gate used by state-mutating debug commands.
    if action in ("help", "commands"):
        return "\n".join(_full_help_lines())
    # The profile flag controls both the client's console UI and the server
    # endpoint.  Do not rely on the client hiding the backtick console: a
    # client can still submit a chat command directly.
    if "allowcon" not in getattr(hconnect_server, "PROFILE_FEATURE_FLAGS", ()):
        return "Developer console is disabled"
    if not parts:
        return ("Commands: !version !arena-cleanup !help !game_end !encounter !hand !zones !playable !gencard "
                "!update !threshold !resource !pass !phase !draw !discard "
                "!addcard !top !issue <title>")

    # Accept both the historical ``!command`` spelling and the slash spelling
    # used by the in-client developer console.  Keep the canonical command
    # names singular internally so old scripts continue to work.
    action = parts[0].lower().lstrip("!/")
    if action == "thresholds":
        action = "threshold"
    elif action == "resources":
        action = "resource"
    args = parts[1:]
    import sys
    print(f"  [CMD DEBUG] action={action} args={args}", file=sys.stderr, flush=True)

    # !game_end and !encounter operate on the campaign layer — handle them
    # before the session gate so they work from the panorama too.
    if action == "game_end":
        try:
            return _cmd_game_end(handler, args)
        except Exception as e:
            return f"Error: {e}"
    if action == "encounter":
        try:
            return _cmd_encounter(handler, args)
        except Exception as e:
            return f"Error: {e}"
    if action == "challenge":
        try:
            return _cmd_challenge(handler, args)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"Error: {e}"
    if action == "reload":
        return reload_runtime_modules()

    player_uid = encoder.make_uid(hconnect_server.UID_TYPE["ServicePlayer"], int(handler.client_reck_id))
    session = game_session.find_session_by_player(player_uid)
    if not session:
        return "No active game"

    pl_t = game_engine.UID.make(244, int(handler.client_reck_id))
    ai_t = game_engine.UID.make(3, 1000)
    # Tournament game_cards are keyed by the ServicePlayer/reckoning id;
    # campaign/practice cards use the local profile id.  Debug commands must
    # use the same owner key as the active game or they can create a phantom
    # tournament participant when a card is generated.
    is_tourney = session and (session.session_name or "").startswith("tourney-")
    command_owner_id = (int(handler.client_reck_id) if is_tourney
                        else handler.user_profile["id"])

    try:
        result = _dispatch(handler, action, args, session, pl_t, ai_t, room, username)
    except Exception as e:
        return f"Error: {e}"
    # Refresh playability for Practice/campaign commands.  Tournament PvP has
    # a separate two-player state machine; loading the PvE battle state here
    # would read a default state and push stale options/priority back to one
    # client after a debug command.
    if not is_tourney:
        try:
            import battle_engine as _be
            bstate = _be.load_state(session)
            phase = _be.current_phase(bstate)
            if bstate.get("turn_player") == _be.PLAYER:
                if phase in (game_engine.ETurnPhases.FirstMainPhase,
                             game_engine.ETurnPhases.SecondMainPhase):
                    handler._push_main_phase_options(session, pl_t, ai_t)
                else:
                    handler._push_phase_options_empty(session, pl_t, ai_t)
        except Exception:
            pass
    return result


def _send_chat(handler, msg, room, username):
    """Send a chat message as the server."""
    from datetime import datetime
    now = datetime.now().strftime("[%H:%M]")
    echo = _json.dumps({"action": "rchat", "room": room, "rflg": "",
                         "user": f"Server {now}", "msg": msg, "flags": "", "icon": ""})
    handler.scnt += 1
    handler.send({"issuer": "Session", "target": "chat", "sid": handler.sid},
                  body=echo.encode("utf-8"))


def _send_game_events(handler, game, session, pl_t):
    """Send a network packet from game events."""
    if not game.events:
        return
    SVC_GS = hconnect_server.SERVICE_GAME_SESSION_UID
    pkt = game.make_network_packet(pl_t)
    dw = encode_datawrapper(0, 3055, compress_gzip(encode_sync_event(pkt)), 1,
                             "00000000-0000-0000-0000-000000000000")
    handler.scnt += 1
    headers = {
        "issuer": f"0.0.0.0.ServiceGameSession.{SVC_GS}.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }
    handler.send(headers, dw)
    handler._event_q.append((handler.scnt, dw, headers))
    if len(handler._event_q) > 100:
        handler._event_q = handler._event_q[-50:]


def _refresh_pvp_debug_options(tournament_game, session, state):
    """Rebuild the current PvP option projection after a debug state change."""
    tournament_game.pvp_push_current_phase_options(session, state)


def _cmd_encounter(handler, args):
    """Launch an encounter battle: !encounter <encounter_guid>.

    Skips the panorama conversation flow and pushes a gamestarted notification
    directly so the client transitions to the battle scene. Works from the
    panorama or anywhere else.
    """
    if not args:
        return "Usage: !encounter <guid>  — see ENCOUNTERS.md for GUIDs"
    db = hconnect_server._db
    encounter_guid = args[0]

    # Find the player's champion and campaign
    uid = handler.user_profile["id"]
    from profile_db import db_latest_champion_for_user
    champ = db_latest_champion_for_user(uid, conn=db)
    if not champ:
        return "No champion found — create one first"
    champ_id, deck_db_id = champ[0], champ[1]
    deck_uid64 = (deck_db_id << 8) | 17 if deck_db_id else 0

    from pve_db import db_latest_campaign_any
    camp = db_latest_campaign_any(champ_id, conn=db)
    camp_id = camp[0] if camp else 0

    import campaign
    campaign._launch_encounter(handler, db, camp_id, champ_id, encounter_guid,
                               deck_uid64, 0, "00000000-0000-0000-0000-000000000000",
                               "ServiceCampaign", str(hconnect_server.UID_TYPE["ServiceCampaign"]),
                               0, hconnect_server.SERVICE_MAIL_UID)
    return f"Launched encounter {encounter_guid} (camp={camp_id})"


def _cmd_game_end(handler, args):
    """End the current battle: !game_end victory|defeat.

    Pushes the battle GameEnded event (shows the Victory/Defeat screen in the
    client) AND the campaign gameendnotify (updates campaign state).
    """
    db = hconnect_server._db
    result = args[0].lower() if args else "victory"
    won = result in ("win", "won", "victory", "winlose", "true", "1")
    out = []
    campaign_handled = False

    # 1) Battle GameEnded event so the client leaves the battle UI.
    player_uid = encoder.make_uid(hconnect_server.UID_TYPE["ServicePlayer"],
                                  int(handler.client_reck_id))
    session = game_session.find_session_by_player(player_uid)
    if session:
        try:
            # ArenaClient immediately joins the lobby after GameEnded.  Commit
            # the FRA result first so that JoinCampaignArena cannot observe the
            # pre-result challenger index and return the same fight.  Reward
            # conversations remain deferred until after GameEnded so the
            # Arena UI is subscribed when it receives them.
            prepared = campaign.prepare_fra_battle_gameend(
                handler, db, session, won)
            if prepared is not None:
                fra_handled = bool(prepared.get("handled"))
                _push_battle_game_end(handler, session, won)
                out.append(
                    f"GameEnded pushed to session {session.session_id} ({result})")
                campaign.publish_fra_battle_gameend(
                    handler, prepared, hconnect_server.SERVICE_MAIL_UID)
                out.append(
                    "FRA battle result applied"
                    if fra_handled else "FRA battle result was not applied")
            else:
                _push_battle_game_end(handler, session, won)
                out.append(
                    f"GameEnded pushed to session {session.session_id} ({result})")

                # Non-FRA campaign battles need the complete result path: it
                # applies authored rewards, advances quest state, sends
                # gameendnotify, and removes the finished session/cards.
                if str(session.session_name or "").startswith("camp_"):
                    campaign_handled = True
                    handled = campaign.handle_battle_gameend(
                        handler, db, session, won,
                        hconnect_server.SERVICE_MAIL_UID,
                        hconnect_server.UID_TYPE["ServiceCampaign"])
                    out.append(
                        "Campaign battle result applied"
                        if handled else "Campaign battle result was not applied")
        except Exception as e:
            out.append(f"GameEnded error: {e}")
    else:
        out.append("No active battle session")

    # 2) Campaign gameendnotify — updates campaign state (reveals quest NPC on a win).
    from pve_db import db_latest_campaign_for_user
    camp_row = db_latest_campaign_for_user(
        handler.user_profile["id"], conn=db)
    if not camp_row:
        out.append("No active campaign for this player")
    elif not campaign_handled:
        camp_id = camp_row[0]
        msg = campaign.push_gameendnotify(
            handler, db, camp_id, won, 0, "00000000-0000-0000-0000-000000000000",
            "ServiceCampaign", str(hconnect_server.UID_TYPE["ServiceCampaign"]), 0,
            hconnect_server.SERVICE_MAIL_UID)
        out.append(f"Campaign {camp_id}: {msg}")
    return "; ".join(out)


def _cmd_challenge(handler, args):
    """Challenge a friend to a duel: !challenge <player_name>"""
    import sys as _sys
    _hcs = _sys.modules.get("hconnect_server") or _sys.modules.get("__main__")
    if _hcs is None:
        return "The HConnect server module is unavailable"
    if not args:
        return "Usage: !challenge <player_name>"

    db = _hcs._db
    opp_name = " ".join(args)
    my_name = handler.user_profile.get("name", "Unknown") if handler.user_profile else "Unknown"
    my_id = handler.user_profile["id"] if handler.user_profile else 0

    # Look up opponent
    from profile_db import db_find_user_by_name, db_latest_deck_for_user
    opp_row = db_find_user_by_name(opp_name)
    if not opp_row:
        return f"Player '{opp_name}' not found"

    opp_id = opp_row[0]
    opp_name = opp_row[1]

    # Check opponent is online via realtime dict from sys.modules
    live_active = _hcs._active_clients
    active = live_active.get(opp_id, [])
    if not active:
        return f"{opp_name} is not online"

    opp_handler = active[0][0]

    # Get challenger's deck
    my_deck_id = db_latest_deck_for_user(my_id, conn=db)
    my_deck_uid64 = (my_deck_id << 8) | 17 if my_deck_id else 0

    # Get opponent's deck
    opp_deck_id = db_latest_deck_for_user(opp_id, conn=db)
    opp_deck_uid64 = (opp_deck_id << 8) | 17 if opp_deck_id else 0

    # Create game session
    import game_session as gs
    my_uid = encoder.make_uid(_hcs.UID_TYPE["ServicePlayer"], int(handler.client_reck_id))
    session_name = f"Challenge_{my_name}_vs_{opp_name}"
    session = gs.create_encounter_session(session_name, {}, my_uid)
    session.add_player(encoder.make_uid(_hcs.UID_TYPE["ServicePlayer"], opp_id), 1)
    session.set_state("joined")
    sess_uid = int(session.session_id)
    room_id = int(session_id_fallback(session))

    # Push 25072 + 25060 to both players
    from encoder import encode_objfmt_response
    _challenge_push_25072_25060(handler, room_id, sess_uid, session_name, my_deck_uid64, my_id, my_name)
    _challenge_push_25072_25060(opp_handler, room_id, sess_uid, session_name, opp_deck_uid64, opp_id, opp_name)

    return f"Challenged {opp_name}! Game session {sess_uid} created."


def session_id_fallback(session):
    """Get a numeric session ID for tournament push compatibility."""
    if hasattr(session, 'session_id') and hasattr(session.session_id, 'uid64'):
        return int(session.session_id.uid64)
    return int(session.session_id)


def _challenge_push_25072_25060(h, room_id, sess_uid, session_name, deck_uid64, player_id, player_name):
    """Push DeckConstructionStarted (25072) + TournamentSessionStart (25060) to one player."""
    import sys as _sys
    _hcs = _sys.modules.get("hconnect_server") or _sys.modules.get("__main__")
    if _hcs is None:
        return
    from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper

    # 25072 — sets CurrentTournament
    dcs_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.DeckConstructionStartedEventArgs",
         "Game.Shared.Tournaments.TournamentInfo",
         "Game.Shared.Domain.deck_bits"],
        [("TournamentID", "ulong", room_id),
         ("TournamentInfo", "struct",
          ("Game.Shared.Tournaments.TournamentInfo",
           [("TournamentID", "ulong", room_id)])),
         ("my_Deck", "class", "Game.Shared.Domain.deck_bits"),
         ("timeForSideboarding", "long", 0),
         ("PlayerID", "ulong", player_id)])

    dcs_body = compress_gzip(dcs_inner)
    dcs_dw = encode_datawrapper(0, 25072, dcs_body, 1,
                                "00000000-0000-0000-0000-000000000000")
    h.scnt += 1
    h.send({
        "issuer": str(_hcs.SERVICE_MAIL_UID),
        "target": "ServicePlayer", "instance": h.sid or "0",
        "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
    }, dcs_dw)

    # 25060 — transition to Battle
    enc_flags = 1024 | 4096 | 8192  # IsImmortalPvP | IsStandardPvP | IsDuelingPit
    evt_inner = encode_objfmt_response(
        ["Game.Shared.Network.Tournaments.TournamentSessionStartEventArgs",
         "Game.Shared.SessionState",
         "Game.Shared.SessionStateEncounterData",
         "Game.Shared.UID"],
        [("SessionState", "struct",
          ("Game.Shared.SessionState",
           [("SessionId", "uid", sess_uid),
            ("SessionName", "string", session_name),
            ("MinimumPlayerCount", "int", 2),
            ("MaximumPlayerCount", "int", 2),
            ("EncounterData", "struct",
             ("Game.Shared.SessionStateEncounterData",
              [("SessionFlags", "int", enc_flags),
               ("IsVirtualTournament", "bool", True),
               ("TournamentID", "ulong", room_id),
               ])),
            ("JoinInsteadOfReconnect", "bool", True)])),
         ("DeckId", "uid", deck_uid64),
         ("Forced", "bool", True)])

    evt_body = compress_gzip(evt_inner)
    evt_dw = encode_datawrapper(0, 25060, evt_body, 1,
                                "00000000-0000-0000-0000-000000000000")
    h.scnt += 1
    h.send({
        "issuer": str(_hcs.SERVICE_MAIL_UID),
        "target": "ServicePlayer", "instance": h.sid or "0",
        "reqid": 0, "c": 0, "conh": 0, "sid": h.sid,
    }, evt_dw)


def _push_battle_game_end(handler, session, won):
    """Push a GameEndedSessionEventArgs (class 2) event for the current battle.

    Winner/loser UID lists wrapped in a NetworkPacketSessionEventArgs pushed on
    the 3055 channel so the client shows the Victory/Defeat screen.
    """
    pl_uid = game_engine.UID.make(244, int(handler.client_reck_id))
    ai_uid = game_engine.UID.make(3, 1000)
    if won:
        push_battle_game_end(handler, session, [pl_uid], [ai_uid])
    else:
        push_battle_game_end(handler, session, [ai_uid], [pl_uid])


def push_battle_game_end(handler, session, winners, losers):
    """Encode and send a GameEnded event for a battle session on the 3055 channel."""
    pl_uid = game_engine.UID.make(244, int(handler.client_reck_id))
    nw = game_engine.make_game_ended_packet(session.session_id, pl_uid,
                                                winners, losers)
    ge_bytes = compress_gzip(encode_sync_event(nw))
    ge_dw = encode_datawrapper(0, 3055, ge_bytes, 1,
                               "00000000-0000-0000-0000-000000000000")
    handler.scnt += 1
    handler.send({
        "issuer": f"0.0.0.0.ServiceGameSession.{hconnect_server.SERVICE_GAME_SESSION_UID}.{session.session_id}.{handler.scnt}",
        "target": "ServiceGameSession", "instance": str(session.server_id),
        "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
    }, ge_dw)
    session.set_state("ended")


def _push_card_update(handler, db, session, pl_t, card_id, user_id=None, **overrides):
    """Fetch card info from DB and send a CardUpdated event.

    Works for both player and AI cards (user_id=0 for the AI). Template data
    resolves via game_cards.template_guid — one path for instance-based and
    GUID cards.
    """
    if user_id is None:
        user_id = handler.user_profile["id"]
    from pvp_db import (db_card_command_info, db_template_projection,
                        db_deck_active_gems)
    row = db_card_command_info(session.session_id, user_id, card_id, conn=db)
    if not row:
        return
    instance_id = row[0]
    zone_str = row[1] or 'Deck'
    ZONE_MAP = {'deck': 1, 'hand': 2, 'void': 32, 'discard': 16, 'warzone': 8,
                 'playedresources': 64, 'underground': 256, 'champions': 4}
    zone_val = ZONE_MAP.get(zone_str.lower(), 1)
    tpl_guid = "00000000-0000-0000-0000-000000000000"
    ct = game_engine.ECardTypes.Troop
    cost, atk, def_ = 0, 0, 0
    if row[2]:
        trow = db_template_projection(row[2], conn=db)
        if trow:
            tpl_guid = row[2]
            ct = game_engine.card_type_from_db(trow[0])
            cost, atk, def_ = trow[1] or 0, trow[2] or 0, trow[3] or 0
    # Fetch thresholds, abilities, and gems
    shards = []
    abilities = []
    gem_type = 0
    if tpl_guid != "00000000-0000-0000-0000-000000000000":
        srow = db_template_projection(tpl_guid, conn=db)
        if srow:
            if srow[4]:
                try:
                    td = _json.loads(srow[4])
                    shard_flags_map = {0:0, 1:4, 2:8, 3:16, 4:32, 5:64}
                    raw_list = td.get('list', [])
                    shards = [value for s in raw_list
                              if (value := shard_flags_map.get(s, s)) is not None]
                except: pass
            if srow[5]:
                try:
                    abilities = [game_engine.ResourceId.from_str(g) for g in _json.loads(srow[5])]
                except: pass
        # Fetch gems from deck
        arena_k = hconnect_server.db_get_arena_state(handler.user_profile["id"])
        deck_k_id = handler._resolve_fra_deck_id(arena_k["deck_id"]) or 0
        gem_value = db_deck_active_gems(deck_k_id, conn=db)
        if gem_value:
            try:
                gems = _json.loads(gem_value)
                gem_type = int(gems.get(str(instance_id), 0)) if gems else 0
            except: pass
    scid = game_engine.SessionCardId(game_engine.UID(card_id))
    ai_t = game_engine.UID.make(3, 1000)
    game = game_engine.Game(session.session_id, pl_t, ai_t)
    game.card_defs[scid] = game_engine.CardDef("Card", ct, cost, atk, def_, shards, abilities)
    kwargs = {'attack': atk, 'defense': def_, 'cost': cost, 'template_id': tpl_guid, 'gems': gem_type}
    kwargs.update(overrides)
    attr_override = kwargs.pop('attributes', None)
    state_val = kwargs.pop('state', game_engine.ECardStates.None_)
    collection_override = kwargs.pop('collection_override', None)
    if collection_override is not None:
        zone_val = collection_override
    game.push_card_updated(scid, pl_t, zone_val, ct, state=state_val, **kwargs)
    if attr_override is not None and game.events:
        game.events[-1].attributes = attr_override
    _send_game_events(handler, game, session, pl_t)


def _dispatch(handler, action, args, session, pl_t, ai_t, room, username):
    db = hconnect_server._db
    # Tournament game_cards are keyed by the ServicePlayer/reckoning id;
    # campaign/practice cards use the local profile id.  Keep debug commands
    # on the same owner key as the active game.
    is_tourney = session and (session.session_name or "").startswith("tourney-")
    command_owner_id = (int(handler.client_reck_id) if is_tourney
                        else handler.user_profile["id"])

    if action == "draw":
        count = max(1, int(args[0]) if args else 1)
        if is_tourney:
            from services.tournament_game import pvp_debug_draw
            drew = pvp_debug_draw(handler, session, count)
            return f"Drew {drew} cards"
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        drew = 0
        for _ in range(count):
            from pvp_db import db_deck_card_count
            if db_deck_card_count(session.session_id, command_owner_id, conn=db) == 0:
                break
            handler._player_draw_card(game, session, pl_t, command_owner_id)
            drew += 1
        _send_game_events(handler, game, session, pl_t)
        return f"Drew {drew} cards"

    elif action == "phase":
        pn = args[0] if args else "StartTurn"
        pv = getattr(game_engine.ETurnPhases, pn, game_engine.ETurnPhases.StartTurn)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.push_turn_phase(pv, pl_t, pl_t)
        _send_game_events(handler, game, session, pl_t)
        return f"Phase set to {pn}"

    elif action == "addcard":
        # Draw the next copy of a card (by name or card_uid) that is still in the
        # deck to hand.
        if not args:
            return "Usage: addcard <name|id>"
        a = args[0]
        al = a.lower()
        target = None
        # Import both lookup helpers before branching.  The name path used to
        # import ``db_deck_card_by_name`` only inside the numeric branch, so a
        # normal ``!addcard Grave Nibbler`` request raised an UnboundLocalError
        # instead of performing the lookup.
        from pvp_db import db_deck_card_by_uid, db_deck_card_by_name
        try:
            uid_int = int(al)
            row = db_deck_card_by_uid(
                session.session_id, command_owner_id, uid_int, conn=db)
            if row:
                target = row
        except ValueError:
            target = db_deck_card_by_name(
                session.session_id, command_owner_id, al, conn=db)
        if not target:
            return f"No copy of '{a}' left in deck"
        card_uid, tpl_guid, card_tpl_id = target
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        handler._move_deck_to_hand(game, session, pl_t, card_uid, tpl_guid, card_tpl_id)
        _send_game_events(handler, game, session, pl_t)
        return f"Added {a} to hand"

    elif action == "discard":
        # Trigger the discard effect directly: pick a random hand card and move
        # it to the discard zone (DiscardCardAbilityEffectTemplate behaviour).
        import random as _rnd
        from pvp_db import db_hand_cards_for_discard, db_card_original_owner_id
        hand_rows = [(row[1], row[2]) for row in db_hand_cards_for_discard(
            session.session_id, handler.user_profile["id"], conn=db)]
        if not hand_rows:
            return "No cards in hand"
        row = _rnd.choice(hand_rows)
        card_uid, tpl_guid = row[0], row[1]
        # Discard to the card's OWNER (a Mind Grasp steal returns to the AI's
        # graveyard — user_id is the controller, owner_user_id the true owner).
        owner_uid = db_card_original_owner_id(
            session.session_id, card_uid, conn=db)
        if owner_uid is None:
            owner_uid = handler.user_profile["id"]
        from pvp_db import db_discard_card
        db_discard_card(session.session_id, card_uid, owner_user_id=owner_uid)
        owner_player_uid = ai_t if owner_uid == 0 else pl_t
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        # Populate the CardDef with full thresholds + active gems so the graveyard
        # view renders the card completely (not just name/type/stats).
        tpl_d, ct_d, name_d, cost_d, atk_d, def_d, gem_d = handler._card_full_data(
            game, scid, tpl_guid, None)
        game.push_card_discarded(scid, owner_player_uid)
        game.push_card_updated(scid, owner_player_uid, game_engine.ECardCollections.Discard,
                               game_engine.card_type_from_db(ct_d) if ct_d else game_engine.ECardTypes.Troop,
                               attack=atk_d, defense=def_d, cost=cost_d,
                               template_id=tpl_d, gems=gem_d)
        game.push_card_moved(scid, owner_player_uid, game_engine.ECardCollections.Discard,
                             game_engine.ECardLocations.Top, 0)
        game.push_player_updated(pl_t, champ_id=getattr(handler, "_player_champ_scid", None))
        game.push_green_light(pl_t, game_engine.EPriorityContext.Normal)
        _send_game_events(handler, game, session, pl_t)
        return f"Discarded a card ({len(hand_rows)} in hand)"

    elif action == "top":
        # Move one of the human player's hand cards to position zero of their
        # deck.  Accept the card UID or a case-insensitive name/substring, as
        # !addcard does, so !hand output can be used directly.
        if not args:
            return "Usage: !top <card_id|name>"
        selector = " ".join(args).strip()
        target = None
        from pvp_db import (db_hand_card_by_uid, db_hand_card_by_name,
                            db_move_hand_card_to_deck_top)
        try:
            card_uid = int(selector)
        except ValueError:
            card_uid = None
        if card_uid is not None:
            target = db_hand_card_by_uid(
                session.session_id, command_owner_id, card_uid, conn=db)
        else:
            target = db_hand_card_by_name(
                session.session_id, command_owner_id, selector, exact=True, conn=db)
            if target is None:
                target = db_hand_card_by_name(
                    session.session_id, command_owner_id, selector, exact=False,
                    conn=db)
        if target is None:
            return f"No card matching '{selector}' in hand"

        card_uid, tpl_guid, card_tpl_id, card_name = target
        from pvp_db import db_move_hand_card_to_deck_top
        db_move_hand_card_to_deck_top(
            session.session_id, command_owner_id, int(card_uid), conn=db)
        db.commit()

        owner_player_uid = (pl_t if not is_tourney
                            else game_engine.UID.make(244, command_owner_id))
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        tpl_d, ct_d, _name_d, cost_d, atk_d, def_d, gem_d = \
            handler._card_full_data(game, scid, tpl_guid, card_tpl_id)
        card_type = (game_engine.card_type_from_db(ct_d)
                     if isinstance(ct_d, str) else ct_d)
        game.push_card_moved(scid, owner_player_uid,
                             game_engine.ECardCollections.Deck,
                             game_engine.ECardLocations.Top, 0)
        game.push_card_updated(scid, owner_player_uid,
                               game_engine.ECardCollections.Deck, card_type,
                               template_id=tpl_d, cost=cost_d,
                               attack=atk_d, defense=def_d, gems=gem_d,
                               state=0, nulling=True)
        _send_game_events(handler, game, session, pl_t)
        return f"Put {card_name} on top of deck"

    elif action == "pass":
        TURN_PHASES = [
            game_engine.ETurnPhases.FirstMainPhase,
            game_engine.ETurnPhases.DeclareCombatPriorityWindow,
            game_engine.ETurnPhases.DeclareAttack,
            game_engine.ETurnPhases.DeclareAttackPriorityWindow,
            game_engine.ETurnPhases.DeclareDefense,
            game_engine.ETurnPhases.DeclareDefensePriorityWindow,
            game_engine.ETurnPhases.AssignFirstStrikeDamage,
            game_engine.ETurnPhases.FirstStrikePriorityWindow,
            game_engine.ETurnPhases.AssignDamage,
            game_engine.ETurnPhases.SecondMainPhase,
            game_engine.ETurnPhases.EndPhase,
            game_engine.ETurnPhases.Discard,
            game_engine.ETurnPhases.EndTurn,
        ]
        if not hasattr(session, 'current_phase_idx'):
            session.current_phase_idx = 0
        else:
            session.current_phase_idx += 1
        idx = session.current_phase_idx % len(TURN_PHASES)
        phase = TURN_PHASES[idx]
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.push_turn_phase(phase, pl_t, pl_t)
        _send_game_events(handler, game, session, pl_t)
        return f"Phase: {idx}.{phase}"

    elif action == "hand":
        target = args[0].lower() if args else "me"
        user_id = handler.user_profile["id"] if target != "opp" else 0
        from pvp_db import db_hand_display_rows
        rows = db_hand_display_rows(session.session_id, user_id, conn=db)
        lines = [f"{r[1]} [{r[0]}]" for r in rows]
        return f"{target} hand: " + ", ".join(lines)

    elif action == "aihand":
        # Reveal all AI hand cards to the player (push CardUpdated with nulling=False)
        from pvp_db import db_ai_hand_template_rows
        rows = db_ai_hand_template_rows(session.session_id, conn=db)
        if not rows:
            return "AI hand is empty"
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        lines = []
        for uid, tpl in rows:
            scid = game_engine.SessionCardId(game_engine.UID(uid))
            t = handler._template_by_guid(tpl)
            ct = game_engine.card_type_from_db(t[1]) if t else game_engine.ECardTypes.Troop
            handler._card_full_data(game, scid, tpl)
            game.push_card_updated(scid, ai_t, game_engine.ECardCollections.Hand, ct,
                                   template_id=tpl, nulling=False)
            lines.append(f"{t[2] if t else 'Card'} [{uid}]")
        _send_game_events(handler, game, session, pl_t)
        return f"AI hand: " + ", ".join(lines)

    elif action == "playable":
        filter_ids = set()
        filter_names = []
        for a in args:
            try:
                filter_ids.add(int(a))
            except ValueError:
                filter_names.append(a.lower())
        name_filter = " ".join(filter_names) if filter_names else ""
        from pvp_db import db_playable_hand_rows
        rows = db_playable_hand_rows(
            session.session_id, handler.user_profile["id"], conn=db)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        playable = []
        for row in rows:
            scid = game_engine.SessionCardId(game_engine.UID(row[0]))
            name = row[1] or ""
            uid = row[0]
            if not args or uid in filter_ids or (name_filter and name_filter in name.lower()):
                playable.append(scid)
        game.push_options(pl_t, playable)
        _send_game_events(handler, game, session, pl_t)
        lbl = f" (filter: {name_filter})" if name_filter else " (all)"
        return f"Playable: {len(playable)}/{len(rows)} cards" + lbl

    elif action == "threshold":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            vals = [max(0, int(a)) for a in args[1:7]]
        else:
            target = "me"
            vals = [max(0, int(a)) for a in args[0:6]]
        while len(vals) < 6:
            vals.append(0)
        if (session.session_name or "").startswith("tourney-"):
            from pvp_db import db_game_session_pids
            from services import tournament_game as _tg
            state = _tg.pvp_load_state(session) or {}
            pids = db_game_session_pids(session.session_id)
            changed_pid = (int(handler.client_reck_id) if target == "me"
                           else next((int(pid) for pid in pids
                                      if int(pid) != int(handler.client_reck_id)),
                                     int(handler.client_reck_id)))
            state[f"thresh_{changed_pid}"] = {
                flag: count for flag, count in zip(
                    [1, 4, 8, 16, 32, 64], vals) if count
            }
            _tg.pvp_save_state(session, state)
            _tg._pvp_sync_game_state(session)
            _refresh_pvp_debug_options(_tg, session, state)
            return f"Thresholds: {vals} for {target}"

        uid = pl_t if target == "me" else ai_t
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        thresholds = {
            game_engine.ECardShards.Colorless: vals[0],
            game_engine.ECardShards.Blood: vals[1],
            game_engine.ECardShards.Ruby: vals[2],
            game_engine.ECardShards.Sapphire: vals[3],
            game_engine.ECardShards.Wild: vals[4],
            game_engine.ECardShards.Diamond: vals[5],
        }
        import battle_engine as _be
        bstate = _be.load_state(session)
        threshold_key = "player_threshold" if target == "me" else "ai_threshold"
        previous = dict(bstate.get(threshold_key) or {})
        bstate[threshold_key] = thresholds
        _be.save_state(session, bstate)
        game.player_threshold = dict(bstate.get("player_threshold") or {})
        game.ai_threshold = dict(bstate.get("ai_threshold") or {})
        for cs_val, value in zip([1, 4, 8, 16, 32, 64], vals):
            old_value = int(previous.get(cs_val,
                                         previous.get(str(cs_val), 0)) or 0)
            if old_value != value:
                ev = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                ev.player_id = uid; ev.color = cs_val
                ev.operation = 1 if value > old_value else 2
                ev.delta = abs(value - old_value); ev.new_value = value
                game._push(ev)
        game.push_player_updated(uid, champ_id=getattr(handler, "_player_champ_scid" if target == "me" else "_ai_champ_scid", None))
        _send_game_events(handler, game, session, pl_t)
        return f"Thresholds: {vals} for {target}"

    elif action == "charge":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            val = int(args[1]) if len(args) > 1 else 0
        else:
            target = "me"
            val = int(args[0]) if args else 0
        uid = pl_t if target == "me" else ai_t
        if (session.session_name or "").startswith("tourney-"):
            from pvp_db import db_game_session_pids
            from services import tournament_game as _tg
            state = _tg.pvp_load_state(session) or {}
            pids = db_game_session_pids(session.session_id)
            changed_pid = (int(handler.client_reck_id) if target == "me"
                           else next((int(pid) for pid in pids
                                      if int(pid) != int(handler.client_reck_id)),
                                     int(handler.client_reck_id)))
            state[f"chg_{changed_pid}"] = val
            _tg.pvp_save_state(session, state)
            _tg._pvp_sync_game_state(session)
            _refresh_pvp_debug_options(_tg, session, state)
            return f"Charges set to {val} for {target}"

        # Persist to battle state so playability/affordability reflects it.
        import battle_engine as _be
        bstate = _be.load_state(session)
        bstate["player_charges" if target == "me" else "ai_charges"] = val
        _be.save_state(session, bstate)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.player_charges = bstate.get("player_charges", 0)
        game.ai_charges = bstate.get("ai_charges", 0)
        ev = game_engine.ChampionChargePointsChangedSessionEventArgs()
        ev.player_id = uid; ev.operation = 0; ev.delta = val; ev.new_value = val
        game._push(ev)
        game.push_player_updated(uid, champ_id=getattr(handler, "_player_champ_scid" if target == "me" else "_ai_champ_scid", None))
        _send_game_events(handler, game, session, pl_t)
        return f"Charges set to {val} for {target}"
    elif action == "spellpoints":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            val = int(args[1]) if len(args) > 1 else 0
        else:
            target = "me"
            val = int(args[0]) if args else 0
        uid = pl_t if target == "me" else ai_t
        import battle_engine as _be
        bstate = _be.load_state(session)
        bstate["player_spell_points" if target == "me" else "ai_spell_points"] = val
        _be.save_state(session, bstate)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.player_spell_points = bstate.get("player_spell_points", 0)
        ev = game_engine.ChampionSpellPointsChangedSessionEventArgs()
        ev.player_id = uid; ev.operation = 0; ev.delta = val; ev.new_value = val
        game._push(ev)
        game.push_player_updated(uid, champ_id=getattr(handler, "_player_champ_scid" if target == "me" else "_ai_champ_scid", None))
        _send_game_events(handler, game, session, pl_t)
        return f"Spell points set to {val} for {target}"
    elif action == "health":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            val = int(args[1]) if len(args) > 1 else 0
        else:
            target = "me"
            val = int(args[0]) if args else 0
        uid = pl_t if target == "me" else ai_t
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        ev = game_engine.ChampionHealthChangedSessionEventArgs()
        ev.player_id = uid; ev.old_damage_value = val; ev.new_damage_value = val
        game._push(ev)
        _send_game_events(handler, game, session, pl_t)
        return f"Health set to {val} for {target}"
    elif action == "resource":
        target = args[0].lower() if args else ""
        if target in ("me", "opp"):
            avail = max(0, int(args[1])) if len(args) > 1 else 0
            maxr = max(0, int(args[2])) if len(args) > 2 else avail
        else:
            target = "me"
            avail = max(0, int(args[0])) if args else 0
            maxr = max(0, int(args[1])) if len(args) > 1 else avail
        uid = pl_t if target == "me" else ai_t

        # Resource commands must update the authoritative state before
        # emitting HUD events.  Previously this branch only sent transient
        # events from a fresh Game (whose pools default to 0/0), so the next
        # phase/priority refresh restored the old values.
        if (session.session_name or "").startswith("tourney-"):
            from pvp_db import db_game_session_pids
            from services import tournament_game as _tg
            state = _tg.pvp_load_state(session) or {}
            pids = db_game_session_pids(session.session_id)
            changed_pid = (int(handler.client_reck_id) if target == "me"
                           else next((int(pid) for pid in pids
                                      if int(pid) != int(handler.client_reck_id)),
                                     int(handler.client_reck_id)))
            state[f"res_{changed_pid}"] = max(0, avail)
            state[f"res_total_{changed_pid}"] = max(0, maxr)
            _tg.pvp_save_state(session, state)
            _tg._pvp_sync_game_state(session)
            _refresh_pvp_debug_options(_tg, session, state)
            return f"Resources: {avail}/{maxr} for {target}"

        import battle_engine as _be
        bstate = _be.load_state(session)
        current_key = "player_resources" if target == "me" else "ai_resources"
        total_key = "player_total_resources" if target == "me" else "ai_total_resources"
        old_avail = int(bstate.get(current_key, 0) or 0)
        old_maxr = int(bstate.get(total_key, 0) or 0)
        bstate[current_key] = max(0, avail)
        bstate[total_key] = max(0, maxr)
        _be.save_state(session, bstate)

        game = game_engine.Game(session.session_id, pl_t, ai_t)
        game.player_resources = bstate.get("player_resources", 0)
        game.player_total_resources = bstate.get("player_total_resources", 0)
        game.player_threshold = dict(bstate.get("player_threshold") or {})
        game.ai_resources = bstate.get("ai_resources", 0)
        game.ai_total_resources = bstate.get("ai_total_resources", 0)
        game.ai_threshold = dict(bstate.get("ai_threshold") or {})
        if old_avail != avail:
            ev_c = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
            ev_c.player_id = uid
            ev_c.operation = 1 if avail > old_avail else 2
            ev_c.delta = abs(avail - old_avail)
            ev_c.new_value = avail
            game._push(ev_c)
        if old_maxr != maxr:
            ev_t = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
            ev_t.player_id = uid
            ev_t.operation = 1 if maxr > old_maxr else 2
            ev_t.delta = abs(maxr - old_maxr)
            ev_t.new_value = maxr
            game._push(ev_t)
        game.push_player_updated(uid, champ_id=getattr(handler, "_player_champ_scid" if target == "me" else "_ai_champ_scid", None))
        _send_game_events(handler, game, session, pl_t)
        return f"Resources: {avail}/{maxr} for {target}"

    elif action == "gencard":
        if not args:
            return "Usage: gencard <name>"
        name = " ".join(args).lower()
        from pvp_db import (db_gencard_template, db_next_card_uid,
                            db_next_game_card_row_id, db_insert_generated_card)
        trow = db_gencard_template(name, conn=db)
        if not trow:
            return f"No card template matching '{name}'"
        tpl_guid, card_type_str, cost, atk, def_, ab_json, attrs = trow
        # Card UIDs are (instance << 8) | CardType.  Adding one to the raw
        # value can change the UID type (e.g. Card -> Player), which corrupts
        # the client's card cache.  Allocate the next instance with the real
        # Card UID type instead.
        max_cuid = db_next_card_uid(session.session_id, conn=db)
        db_insert_generated_card(
            session.session_id, command_owner_id, max_cuid, tpl_guid, "hand",
            card_type_str, ab_json, attrs,
            db_next_game_card_row_id(session.session_id, conn=db), conn=db,
            position=0, card_state=0, owner_user_id=command_owner_id,
            original_template_guid=tpl_guid)
        db.commit()
        # Sync per-instance data from the template (card_type, abilities, attributes,
        # original_template_guid) — ensures all columns are valid regardless of
        # which path created the row.
        handler._sync_instance_card_data(session, max_cuid, tpl_guid)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        scid = game_engine.SessionCardId(game_engine.UID(max_cuid))
        ct = game_engine.card_type_from_db(card_type_str)
        # Populate CardDef with full card data (abilities, thresholds, etc.)
        handler._card_full_data(game, scid, tpl_guid)
        game.push_card_drawn(scid, pl_t, 1)
        game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand,
                                ct, attack=atk or 0, defense=def_ or 0, cost=cost or 0,
                                template_id=tpl_guid)
        _send_game_events(handler, game, session, pl_t)
        return f"Generated card: {name} ({tpl_guid})"

    elif action == "help":
        lines = _full_help_lines()
        for line in lines:
            _send_chat(handler, line, room, username)
        return ""

    elif action == "zones":
        target = args[0].lower() if args else "me"
        # For PvP sessions, game_cards.user_id stores the reckoning id
        # (not the small profile id), so use client_reck_id directly.
        is_tourney = session and (session.session_name or "").startswith("tourney-")
        if is_tourney:
            my_pid = int(handler.client_reck_id)
            if target == "opp":
                from pvp_db import db_session_user_ids
                rows = [row for row in db_session_user_ids(
                    session.session_id, conn=db) if int(row[0]) != my_pid]
                user_id = rows[0][0] if rows else 0
            else:
                user_id = my_pid
        else:
            user_id = handler.user_profile["id"] if target != "opp" else 0
        from pvp_db import db_zone_display_rows
        all_rows = db_zone_display_rows(session.session_id, user_id, conn=db)
        by_zone = {}
        for r in all_rows:
            zone_name = r[1] or 'Deck'
            card_link = _chat_card_link(r[2], r[3])
            by_zone.setdefault(zone_name, []).append(f"{card_link} [{r[0]}]")
        for z, cards in by_zone.items():
            if cards:
                _send_chat(handler, f"{target} {z} ({len(cards)}): {', '.join(cards)}", room, username)
        return f"{len(by_zone)} zones listed ({target})" if by_zone else f"No cards in session ({target})"

    elif action == "move":
        if len(args) < 2:
            return "Usage: !move <card_id> <zone>"
        card_id = int(args[0])
        zone_name = " ".join(args[1:])
        ZONE_MAP = {'deck': 1, 'hand': 2, 'champions': 4, 'warzone': 8,
                     'discard': 16, 'void': 32, 'playedresources': 64,
                     'castspells': 128, 'underground': 256}
        zone_val = ZONE_MAP.get(zone_name.lower(), 1)
        from pvp_db import db_move_debug_card
        db_move_debug_card(session.session_id, card_id, zone_name, conn=db)
        db.commit()
        # CardMoved first (animation), then CardUpdated (updates cache to new zone)
        game = game_engine.Game(session.session_id, pl_t, ai_t)
        scid = game_engine.SessionCardId(game_engine.UID(card_id))
        game.push_card_moved(scid, pl_t, zone_val, game_engine.ECardLocations.Unknown, 0)
        _send_game_events(handler, game, session, pl_t)
        _push_card_update(handler, db, session, pl_t, card_id, collection_override=zone_val)
        return f"Moved {card_id} to {zone_name} (zone={zone_val})"

    elif action == "update":
        if not args:
            return "Usage: !update <card_id>"
        card_id = int(args[0])
        _push_card_update(handler, db, session, pl_t, card_id)
        return f"Updated card {card_id}"

    elif action == "state":
        if len(args) < 2: return "Usage: !state <card_id> <flags>  (Tapped|Blocking|Attacking|Damaged|Healed|Dead|HasAttacked|HasBlocked|EffectExpired|Activated)"
        card_id = int(args[0])
        state_val = 0
        unknown = []
        for flag in args[1:]:
            for sub_flag in flag.split('|'):
                sub_flag = sub_flag.strip()
                if not sub_flag: continue
                val = getattr(game_engine.ECardStates, sub_flag, None)
                if val is not None:
                    state_val |= val
                else:
                    unknown.append(sub_flag)
        if unknown: return f"Unknown state flags: {', '.join(unknown)}"
        _push_card_update(handler, db, session, pl_t, card_id, state=state_val)
        return f"State set to {state_val} for card {card_id}"
    elif action in ("attr", "attributes"):
        if len(args) < 2: return "Usage: !attr <card_id> <flags>  (Flight|Speed|SkyGuard|Crush|Steadfast|Invincible|SpellShield|Unique|LifeDrain)"
        card_id = int(args[0])
        attr_val = 0
        unknown = []
        for flag in args[1:]:
            for sub_flag in flag.split('|'):
                sub_flag = sub_flag.strip()
                if not sub_flag: continue
                val = getattr(game_engine.ECardAttributes, sub_flag, None)
                if val is not None:
                    attr_val |= val
                else:
                    unknown.append(sub_flag)
        if unknown: return f"Unknown attribute flags: {', '.join(unknown)}"
        _push_card_update(handler, db, session, pl_t, card_id, attributes=attr_val)
        return f"Attributes set to {attr_val} for card {card_id}"
    return "Unknown command. !help for list."
