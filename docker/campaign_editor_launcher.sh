#!/bin/sh
# Start the optional campaign editor when its private submodule source exists.
set -eu

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
EDITOR_SCRIPT="$BASE_DIR/hex-private/campaign/tools/campaign_editor/server.py"

if [ ! -f "$EDITOR_SCRIPT" ]; then
    echo "[campaign_editor] source not found; skipping"
    exit 0
fi

EDITOR_HOST=${HEX_CAMPAIGN_EDITOR_HOST:-127.0.0.1}
EDITOR_PORT=${HEX_CAMPAIGN_EDITOR_PORT:-8765}
if [ -n "${HEX_DB_PATH:-}" ]; then
    EDITOR_DB=$HEX_DB_PATH
else
    EDITOR_DB="$BASE_DIR/hconnect.db"
fi

exec python3 -u "$EDITOR_SCRIPT" \
    --host "$EDITOR_HOST" --port "$EDITOR_PORT" --db "$EDITOR_DB"
