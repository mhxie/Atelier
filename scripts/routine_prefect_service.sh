#!/bin/bash
# launchd bootstrap for the local Prefect API and Atelier deployment runner.

set -euo pipefail

MODE="${1:?usage: routine_prefect_service.sh server|serve}"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
ATELIER_DIR="$(dirname "$SCRIPTS_DIR")"

set +eu
source "$HOME/.zprofile" 2>/dev/null || true
source "$HOME/.profile" 2>/dev/null || true
source "$ATELIER_DIR/harness/env.local.sh" 2>/dev/null || true
set -eu

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) [ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH" ;;
esac

PREFECT_PORT="${ATELIER_PREFECT_PORT:-4200}"
export PREFECT_HOME="${ATELIER_PREFECT_HOME:-$HOME/Library/Application Support/Atelier/Prefect}"
export PREFECT_API_URL="http://127.0.0.1:${PREFECT_PORT}/api"
export PREFECT_SERVER_ANALYTICS_ENABLED="${ATELIER_PREFECT_SERVER_ANALYTICS_ENABLED:-false}"
mkdir -p "$PREFECT_HOME" "$HOME/Library/Logs/Atelier"
cd "$ATELIER_DIR"

# launchd already holds this service's log fds; trim an oversized log in place, keeping its tail.
trim_logs() {
    for log in "$HOME/Library/Logs/Atelier/com.atelier.prefect-$1."{out,err}; do
        { [ -f "$log" ] && [ "$(stat -f %z "$log")" -gt 8388608 ] && tail -c 1048576 "$log" > "$log.prev" && : > "$log"; } || true
    done
}

case "$MODE" in
    server) trim_logs server; exec uv run --frozen prefect server start --host 127.0.0.1 --port "$PREFECT_PORT" ;;
    serve)
        : "${OV:?ERROR: OV must be set in harness/env.local.sh}"
        trim_logs routines
        export ATELIER_ROUTINE_NOTIFY="${ATELIER_ROUTINE_NOTIFY:-1}"
        exec uv run --frozen python scripts/routine_prefect.py serve
        ;;
    *) echo "ERROR: mode must be server or serve" >&2; exit 2 ;;
esac
