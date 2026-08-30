#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Hex TCG private server restart script.
#
# Reliably stops any previously running hconnect server / auth proxy / bridge
# processes, validates all Python sources, starts the services fresh, and
# verifies each port is actually listening before exiting.
#
#   server  : hconnect_server.py  ->  TCP 9933  (HConnect game protocol)
#   proxy   : proxy.py 8081        ->  TCP 8081  (Steam auth / collection HTTP)
#
# Usage:
#   bash restart.sh       # stop and start all services
#   bash restart.sh stop  # stop all services without starting them
# ---------------------------------------------------------------------------
set -euo pipefail

BASE_DIR="/home/ianutley/Hex"
LOG_DIR="/tmp"
SERVER_PORT=9933
PROXY_PORT=8081

# Python files that must compile cleanly before we start anything.
SOURCES=(
    "$BASE_DIR/hconnect_server.py"
    "$BASE_DIR/proxy.py"
    "$BASE_DIR/campaign.py"
    "$BASE_DIR/commands.py"
    "$BASE_DIR/db.py"
    "$BASE_DIR/encoder.py"
    "$BASE_DIR/game_engine.py"
    "$BASE_DIR/game_session.py"
    "$BASE_DIR/objfmt_builder.py"
    "$BASE_DIR/static.py"
    "$BASE_DIR/ability.py"
    "$BASE_DIR/ai.py"
    "$BASE_DIR/battle_engine.py"
    "$BASE_DIR/AssetExtraction/generate_starter_decks.py"
    "$BASE_DIR/campaign_chains/__init__.py"
    "$BASE_DIR/gamemodes/__init__.py"
    "$BASE_DIR/gamemodes/tournament_server.py"
    "$BASE_DIR/services/__init__.py"
    "$BASE_DIR/services/social.py"
    "$BASE_DIR/services/replay.py"
    "$BASE_DIR/replay_server.py"
)
# Also validate every .py file under these packages compiles.
PACKAGES=("domain" "abilities")

log()  { echo "[restart] $*"; }
die()  { echo "[restart] ERROR: $*" >&2; exit 1; }

ACTION="${1:-restart}"
case "$ACTION" in
    restart|stop) ;;
    *) die "unknown action '$ACTION' (use 'restart' or 'stop')" ;;
esac

stop_services() {
    log "Stopping previous processes (hconnect_server, proxy, bridge)..."
    pkill -9 -f "$BASE_DIR/hconnect_server.py" 2>/dev/null || true
    pkill -9 -f "$BASE_DIR/proxy.py" 2>/dev/null || true
    pkill -9 -f "$BASE_DIR/bridge.py" 2>/dev/null || true
    pkill -9 -f "$BASE_DIR/gamemodes/tournament_server.py" 2>/dev/null || true
    pkill -9 -f "$BASE_DIR/replay_server.py" 2>/dev/null || true
    # Also catch plain command-line forms (e.g. launched from another CWD).
    pkill -9 -f "hconnect_server.py" 2>/dev/null || true
    pkill -9 -f "proxy.py 8081" 2>/dev/null || true
    pkill -9 -f "bridge.py" 2>/dev/null || true
    pkill -9 -f "tournament_server.py" 2>/dev/null || true
    pkill -9 -f "replay_server.py" 2>/dev/null || true

    # Belt-and-braces: free the TCP ports from any lingering holder.
    if command -v fuser >/dev/null 2>&1; then
        for port in "$SERVER_PORT" "$PROXY_PORT"; do
            fuser -k -9 "$port"/tcp 2>/dev/null || true
        done
    fi
}

# ---------------------------------------------------------------------------
# 1. Kill every prior instance by name and by port.
# ---------------------------------------------------------------------------
stop_services

if [[ "$ACTION" == "stop" ]]; then
    log "All services stopped."
    exit 0
fi

# Give the OS a moment to release sockets / reap processes.
sleep 2

# ---------------------------------------------------------------------------
# 2. Database — apply pending migrations, or create a fresh database.
# ---------------------------------------------------------------------------
log "Checking database ..."
# Migrations: run migration.py if present, then remove it.
if [[ -f "$BASE_DIR/migration.py" ]]; then
    log "Running migration.py ..."
    python3 "$BASE_DIR/migration.py" || die "migration.py failed"
    rm -f "$BASE_DIR/migration.py"
    log "Migration applied."
fi
# Fresh database: created from static.py if it doesn't exist yet.
if [[ ! -f "$BASE_DIR/hconnect.db" ]]; then
    log "Creating fresh database from static.py ..."
    python3 -c "
import sqlite3, static
db = sqlite3.connect('$BASE_DIR/hconnect.db')
static.ensure_schema(db)
db.close()
" || die "fresh database creation failed"
    log "Database created."
fi

# ---------------------------------------------------------------------------
# 3. Validate all sources before starting.
# ---------------------------------------------------------------------------
log "Compiling sources..."
for src in "${SOURCES[@]}"; do
    if [[ -f "$src" ]]; then
        python3 -m py_compile "$src" || die "syntax error in $src"
    fi
done
for pkg in "${PACKAGES[@]}"; do
    if [[ -d "$BASE_DIR/$pkg" ]]; then
        while IFS= read -r -d '' f; do
            python3 -m py_compile "$f" || die "syntax error in $f"
        done < <(find "$BASE_DIR/$pkg" -name '*.py' -print0)
    fi
done
log "All sources compile OK."

# Starter decks are derived from the client data rather than checked into the
# server repository. Keep a local deployment's generated copy in sync when a
# gamedata file or Records snapshot is available.
STARTER_DECK_GENERATOR="$BASE_DIR/AssetExtraction/generate_starter_decks.py"
if [[ -n "${HEX_GAMEDATA:-}" ]]; then
    [[ -f "$HEX_GAMEDATA" ]] || die "HEX_GAMEDATA does not exist: $HEX_GAMEDATA"
    log "Generating starter decks from HEX_GAMEDATA ..."
    python3 "$STARTER_DECK_GENERATOR" --gamedata "$HEX_GAMEDATA" \
        --output "$BASE_DIR/generated/starter_decks.json" || die "starter-deck generation failed"
elif [[ -d "${HEX_RECORDS:-$BASE_DIR/Records}" ]]; then
    log "Generating starter decks from Records ..."
    python3 "$STARTER_DECK_GENERATOR" --records-dir "${HEX_RECORDS:-$BASE_DIR/Records}" \
        --output "$BASE_DIR/generated/starter_decks.json" || die "starter-deck generation failed"
else
    log "No gamedata or Records source; keeping any existing generated starter decks."
fi

# ---------------------------------------------------------------------------
# 4. Reset logs (keep last 1000 lines) and start detached.
for logfile in "$LOG_DIR/hconnect_log.txt" "$LOG_DIR/proxy_log.txt" "$LOG_DIR/hconnect_requests.log"; do
    if [[ -f "$logfile" ]]; then
        tail -1000 "$logfile" > "$logfile.tmp" && mv "$logfile.tmp" "$logfile"
    else
        : > "$logfile"
    fi
done
echo "=================================" >> "$LOG_DIR/hconnect_log.txt"

log "Starting HConnect server on :$SERVER_PORT ..."
setsid nohup python3 -u "$BASE_DIR/hconnect_server.py" \
    >> "$LOG_DIR/hconnect_log.txt" 2>&1 < /dev/null &
SERVER_PID=$!

log "Starting auth proxy on :$PROXY_PORT ..."
setsid nohup python3 -u "$BASE_DIR/proxy.py" "$PROXY_PORT" \
    >> "$LOG_DIR/proxy_log.txt" 2>&1 < /dev/null &
PROXY_PID=$!

log "Starting tournament server ..."
setsid nohup python3 -u "$BASE_DIR/gamemodes/tournament_server.py" \
    >> "$LOG_DIR/hconnect_log.txt" 2>&1 < /dev/null &

log "Starting replay server ..."
setsid nohup python3 -u "$BASE_DIR/replay_server.py" \
    >> "$LOG_DIR/hconnect_log.txt" 2>&1 < /dev/null &
REPLAY_PID=$!

# ---------------------------------------------------------------------------
# 5. Wait until both processes are alive and their ports accept connections.
# ---------------------------------------------------------------------------
wait_for_port() {
    local port="$1"
    # HConnect imports and seeds the Records-derived metadata before binding;
    # a cold start can legitimately take well over the old 7.5 second window.
    local tries="${2:-120}"
    local i
    for ((i = 1; i <= tries; i++)); do
        if (echo > "/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

log "Waiting for server to listen on :$SERVER_PORT ..."
if ! wait_for_port "$SERVER_PORT"; then
    die "HConnect server failed to bind :$SERVER_PORT (see $LOG_DIR/hconnect_log.txt)"
fi

log "Waiting for proxy to listen on :$PROXY_PORT ..."
if ! wait_for_port "$PROXY_PORT"; then
    die "Proxy failed to bind :$PROXY_PORT (see $LOG_DIR/proxy_log.txt)"
fi

# Confirm the PIDs we spawned are still alive (i.e. didn't crash post-bind).
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    die "HConnect server (PID $SERVER_PID) exited — see $LOG_DIR/hconnect_log.txt"
fi
if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    die "Proxy (PID $PROXY_PID) exited — see $LOG_DIR/proxy_log.txt"
fi
if ! kill -0 "$REPLAY_PID" 2>/dev/null; then
    die "Replay server (PID $REPLAY_PID) exited — see $LOG_DIR/hconnect_log.txt"
fi

# The tournament server runs in-process: hconnect_server.main() calls
# tournament_server.start() (pool seeding + refill scheduler). Verify the
# waiting-room pool was seeded in the DB.
T_POOL=$(sqlite3 "$BASE_DIR/hconnect.db" \
    "SELECT COUNT(*) FROM tournaments WHERE status='waiting'" 2>/dev/null || echo 0)
log "Tournament scheduler: $T_POOL waiting room(s) ready"

log "OK: server PID $SERVER_PID (:9933), proxy PID $PROXY_PID (:8081), replay PID $REPLAY_PID"
