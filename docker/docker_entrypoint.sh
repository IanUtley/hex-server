#!/bin/sh
# Entrypoint for the Fargate container: bootstrap the database, then let
# Supervisor own all long-running server services.
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
: > /tmp/campaign_editor_log.txt

# The campaign editor is published as a container service. Local restart.sh
# keeps the same program bound to loopback by default.
export HEX_CAMPAIGN_EDITOR_HOST="${HEX_CAMPAIGN_EDITOR_HOST:-0.0.0.0}"
export HEX_CAMPAIGN_EDITOR_PORT="${HEX_CAMPAIGN_EDITOR_PORT:-8765}"

# Validate the gamedata mount and create a persistent database when this is a
# first deployment. Services open SQLite only after this has completed.
python3 /hex/docker/docker_bootstrap.py
export HEX_DB_BOOTSTRAPPED=1

echo "[docker] starting Supervisor-managed Hex services"
exec supervisord -n -c /hex/supervisord.conf
