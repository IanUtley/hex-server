#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Hex TCG private server restart script.
#
# Reliably stops any previously running HConnect, auth proxy, tournament and
# replay worker processes.
# processes, validates all Python sources, starts the services fresh, and
# verifies each port is actually listening before exiting.
#
# Only to be used in development
#
#   server  : hconnect_server.py  ->  TCP 9933  (HConnect game protocol)
#   proxy   : proxy.py 8081        ->  TCP 8081  (Steam auth / collection HTTP)
#
# Usage:
#   bash restart.sh       # stop and start all services
#   bash restart.sh stop  # stop all services without starting them
# ---------------------------------------------------------------------------
set -euo pipefail

BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
LOG_DIR="/tmp"
SERVER_PORT=9933
PROXY_PORT=8081

echo ------------------------------------------------------------------------
echo For development purposes only. Please use Docker version for deployment.
echo ------------------------------------------------------------------------

log()  { echo "[restart] $*"; }
die()  { echo "[restart] ERROR: $*" >&2; exit 1; }

compile_python_sources() {
    local source
    while IFS= read -r -d '' source; do
        python3 -m py_compile "$source" || die \
            "syntax error in ${source#"$BASE_DIR/"}"
    done < <(
        find "$BASE_DIR" \
            \( -path "$BASE_DIR/.git" \
               -o -name '.venv' \
               -o -name '.venv-*' \) -prune \
            -o -type f -name '*.py' -print0
    )
}

ACTION="${1:-restart}"
case "$ACTION" in
    restart|stop) ;;
    *) die "unknown action '$ACTION' (use 'restart' or 'stop')" ;;
esac

stop_services() {
    log "Stopping previous processes (hconnect_server, proxy, tournament, replay)..."
    if [[ -f /tmp/hex-supervisord.pid ]]; then
        supervisor_pid="$(< /tmp/hex-supervisord.pid)"
        if [[ "$supervisor_pid" =~ ^[0-9]+$ ]]; then
            kill -TERM "$supervisor_pid" 2>/dev/null || true
        fi
    fi
    pkill -9 -f "$BASE_DIR/hconnect_server.py" 2>/dev/null || true
    pkill -9 -f "$BASE_DIR/proxy.py" 2>/dev/null || true
    pkill -9 -f "$BASE_DIR/gamemodes/tournament_server.py" 2>/dev/null || true
    pkill -9 -f "$BASE_DIR/replay_server.py" 2>/dev/null || true
    # Also catch plain command-line forms (e.g. launched from another CWD).
    pkill -9 -f "hconnect_server.py" 2>/dev/null || true
    pkill -9 -f "proxy.py 8081" 2>/dev/null || true
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
# 2. Database — apply pending migrations and run the shared bootstrap.
# ---------------------------------------------------------------------------
log "Checking database ..."
# Migrations: run migration.py if present, then remove it.
if [[ -f "$BASE_DIR/migration.py" ]]; then
    log "Running migration.py ..."
    python3 "$BASE_DIR/migration.py" || die "migration.py failed"
    rm -f "$BASE_DIR/migration.py"
    log "Migration applied."
fi
# Keep local startup behavior aligned with the Docker entrypoint. This applies
# schema upgrades and server-owned seeds to existing databases, materializes
# Records when gamedata is configured, repairs missing FRA catalogues, and
# validates the complete reference projection before any service opens SQLite.
# Local restart historically did not run the Docker bootstrap test suite; keep
# that behavior unless the caller explicitly invokes the bootstrap itself with
# HEX_RUN_TESTS_ON_BOOT enabled.
log "Running database bootstrap ..."
HEX_RUN_TESTS_ON_BOOT=0 \
    python3 "$BASE_DIR/docker/docker_bootstrap.py" \
    || die "database bootstrap failed"

# ---------------------------------------------------------------------------
# 3. Validate all sources before starting.
# ---------------------------------------------------------------------------
log "Compiling sources..."
compile_python_sources
log "All sources compile OK."

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

if [[ "${HEX_USE_SUPERVISOR:-1}" == "1" ]]; then
    command -v supervisord >/dev/null 2>&1 || die \
        "HEX_USE_SUPERVISOR=1 but supervisord is not installed (pip install -r requirements.txt)"
    log "Starting Supervisor-managed Hex services ..."
    setsid nohup supervisord -n -c "$BASE_DIR/supervisord.conf" \
        >> "$LOG_DIR/hconnect_log.txt" 2>&1 < /dev/null &
    SUPERVISOR_PID=$!
    SERVER_PID=$SUPERVISOR_PID
    PROXY_PID=$SUPERVISOR_PID
    REPLAY_PID=$SUPERVISOR_PID
else
log "Starting HConnect server on :$SERVER_PORT ..."
# The migrated RulesPort is the only transaction path for live client
# sessions; there is no legacy rollback mode.
setsid nohup "$BASE_DIR/run_hconnect.sh" \
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
fi

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
