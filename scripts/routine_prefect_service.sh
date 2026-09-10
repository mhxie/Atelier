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
mkdir -p "$PREFECT_HOME"
cd "$ATELIER_DIR"

case "$MODE" in
    server) exec uv run --frozen prefect server start --host 127.0.0.1 --port "$PREFECT_PORT" ;;
    serve)
        : "${OV:?ERROR: OV must be set in harness/env.local.sh}"
        exec uv run --frozen python scripts/routine_prefect.py serve
        ;;
    *) echo "ERROR: mode must be server or serve" >&2; exit 2 ;;
esac
