#!/bin/sh
# Start the optional campaign editor (campaign_editor/ at the project root)
# when the campaign it edits (campaign/ at the project root, or
# HEX_CAMPAIGN_ROOT) is present.
set -eu

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
EDITOR_SCRIPT="$BASE_DIR/campaign_editor/server.py"

CAMPAIGN_DATA=${HEX_CAMPAIGN_ROOT:-$BASE_DIR/campaign}

if [ ! -f "$EDITOR_SCRIPT" ] || [ ! -d "$CAMPAIGN_DATA/campaign_runtime" ]; then
    echo "[campaign_editor] editor or campaign source not found; skipping"
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
