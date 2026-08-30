#!/bin/sh
# Entrypoint for the Fargate container: start all server services and tail logs
# to stdout.
set -e

# The Docker image is locked down by default.  Keep the local server's
# historical allowcon default unchanged, but make both client feature flags
# opt-in for containers.  An explicitly supplied HEX_PROFILE_FLAGS wins.
if [ -z "${HEX_PROFILE_FLAGS+x}" ]; then
    export HEX_PROFILE_FLAGS=""
fi

# Keep the SQLite database (including its WAL/SHM sidecars) in the configured
# directory. Docker deployments can bind-mount that directory from the host.
if [ -n "${HEX_DB_PATH:-}" ]; then
    mkdir -p "$(dirname "$HEX_DB_PATH")"
fi

mkdir -p /tmp
: > /tmp/hconnect_log.txt
: > /tmp/hconnect_requests.log
: > /tmp/proxy_log.txt
: > /tmp/tournament_log.txt
: > /tmp/replay_log.txt

# Validate the gamedata mount and create a persistent database when this is a
# first deployment. Both services open SQLite only after this has completed.
python3 /hex/docker/docker_bootstrap.py

# Start the HConnect game server (9933).
python3 /hex/hconnect_server.py >> /tmp/hconnect_log.txt 2>&1 &
SERVER_PID=$!

# Start the HTTP proxy (8081).
python3 /hex/proxy.py 8081 >> /tmp/proxy_log.txt 2>&1 &
PROXY_PID=$!

# Start the tournament pool/refill scheduler. It has no listening port; the
# HConnect process handles tournament protocol requests in-process.
python3 /hex/gamemodes/tournament_server.py >> /tmp/tournament_log.txt 2>&1 &
TOURNAMENT_PID=$!

# Build completed session event streams into client replay files and index rows.
python3 /hex/replay_server.py >> /tmp/replay_log.txt 2>&1 &
REPLAY_PID=$!

echo "[docker] hconnect_server pid=$SERVER_PID, proxy pid=$PROXY_PID, tournament pid=$TOURNAMENT_PID, replay pid=$REPLAY_PID"

# Keep the container alive and stream all logs to stdout.
(tail -f /tmp/hconnect_log.txt /tmp/hconnect_requests.log /tmp/proxy_log.txt \
    /tmp/tournament_log.txt /tmp/replay_log.txt) &

# Forward SIGTERM to children.
trap 'kill $SERVER_PID $PROXY_PID $TOURNAMENT_PID $REPLAY_PID 2>/dev/null || true' TERM INT

wait $SERVER_PID $PROXY_PID $TOURNAMENT_PID $REPLAY_PID
