#!/bin/bash
# Owner-gated deterministic refresh for daily-brief reminder inputs.

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
ATELIER_DIR="$(dirname "$SCRIPTS_DIR")"

# launchd does not inherit the interactive shell environment. Temporarily
# relax strict mode while loading user profiles because unrelated variables in
# those files may be unset.
set +eu
source "$HOME/.zprofile" 2>/dev/null || true
source "$HOME/.profile" 2>/dev/null || true
source "$ATELIER_DIR/harness/env.local.sh" 2>/dev/null || true
set -eu

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) [ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH" ;;
esac

: "${OV:?ERROR: OV not set; export it from a login profile or harness/env.local.sh}"

ATELIER_PYTHON="$($SCRIPTS_DIR/find_python.sh)"
TIMEOUT_CMD=(
    "$ATELIER_PYTHON" "$SCRIPTS_DIR/command_timeout.py"
    --seconds "${ATELIER_PREFLIGHT_TIMEOUT_SECONDS:-30}"
    --
)

OWNER_EXIT=0
OWNER_RESULT=$(
    "${TIMEOUT_CMD[@]}" "$ATELIER_PYTHON" "$SCRIPTS_DIR/routine_owner.py" check --json 2>&1
) || OWNER_EXIT=$?
if [ "$OWNER_EXIT" -eq 1 ]; then
    echo "[$(date -Iseconds)] tracking refresh skipped: owned by another machine"
    exit 0
fi
if [ "$OWNER_EXIT" -ne 0 ]; then
    echo "[$(date -Iseconds)] ERROR: routine owner check failed: $OWNER_RESULT" >&2
    exit 2
fi

echo "[$(date -Iseconds)] tracking refresh started"
cd "$ATELIER_DIR"
/usr/bin/caffeinate -i \
    "$ATELIER_PYTHON" "$SCRIPTS_DIR/command_timeout.py" \
    --seconds "${ATELIER_TRACKING_REFRESH_TIMEOUT_SECONDS:-60}" \
    -- \
    "$ATELIER_PYTHON" "$SCRIPTS_DIR/refresh_tracking.py" --json
echo "[$(date -Iseconds)] tracking refresh completed"
