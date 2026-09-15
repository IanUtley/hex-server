#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/home/ianutley/Hex"

if [[ "${HEX_DEBUGPY:-0}" =~ ^(1|true|yes|on)$ ]]; then
    DEBUGPY_BIN="${HEX_DEBUGPY_BIN:-}"
    if [[ -z "$DEBUGPY_BIN" ]]; then
        DEBUGPY_BIN="$(command -v debugpy || true)"
    fi
    if [[ -z "$DEBUGPY_BIN" || ! -x "$DEBUGPY_BIN" ]]; then
        echo "[debugpy] HEX_DEBUGPY=1 but the debugpy executable was not found" >&2
        exit 1
    fi
    DEBUGPY_HOST="${HEX_DEBUGPY_HOST:-127.0.0.1}"
    DEBUGPY_PORT="${HEX_DEBUGPY_PORT:-5678}"
    DEBUGPY_ARGS=(--listen "${DEBUGPY_HOST}:${DEBUGPY_PORT}")
    if [[ "${HEX_DEBUGPY_WAIT:-0}" =~ ^(1|true|yes|on)$ ]]; then
        DEBUGPY_ARGS+=(--wait-for-client)
        echo "[debugpy] waiting for debugger on ${DEBUGPY_HOST}:${DEBUGPY_PORT}"
    else
        echo "[debugpy] listening on ${DEBUGPY_HOST}:${DEBUGPY_PORT}"
    fi
    # The launcher has already created the listener. Prevent the target's
    # optional in-process hook from trying to bind the same port a second time.
    export HEX_DEBUGPY_LAUNCHED=1
    exec "$DEBUGPY_BIN" "${DEBUGPY_ARGS[@]}" \
        "$BASE_DIR/hconnect_server.py" "$@"
fi

exec python3 -u "$BASE_DIR/hconnect_server.py" "$@"
