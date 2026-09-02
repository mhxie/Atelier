#!/bin/bash
# Owner-gated, timeout-bounded runner for a deterministic script that lives in
# the vault. A private launchd plist under $OV/_meta/launchd/ names the job
# label and the vault-relative script; this wrapper supplies what launchd does
# not: login-profile environment, the machine-ownership gate, a wake
# assertion, a hard timeout, and timestamped log lines. No model runs here.
#
# Usage:
#   vault_job_runner.sh <label> <vault-relative-script.py|.sh> [args...]
#
# Environment:
#   OV                                 - vault root (required)
#   ATELIER_VAULT_JOB_TIMEOUT_SECONDS  - hard timeout for the script (default 900)
#   ATELIER_PREFLIGHT_TIMEOUT_SECONDS  - ownership check timeout (default 30)

set -euo pipefail

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

: "${OV:?ERROR: OV not set; export it from a login profile or harness/env.local.sh}"

if [ "$#" -lt 2 ]; then
    echo "usage: $0 <label> <vault-relative-script> [args...]" >&2
    exit 2
fi
LABEL="$1"
SCRIPT_REL="$2"
shift 2

if ! printf '%s' "$LABEL" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*$'; then
    echo "ERROR: invalid job label: $LABEL" >&2
    exit 2
fi
case "$SCRIPT_REL" in
    /*|*..*) echo "ERROR: script must be a vault-relative path without '..': $SCRIPT_REL" >&2; exit 2 ;;
esac
SCRIPT_ABS="$OV/$SCRIPT_REL"
if [ ! -f "$SCRIPT_ABS" ]; then
    echo "ERROR: vault script not found: $SCRIPT_ABS" >&2
    exit 2
fi

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
    echo "[$(date -Iseconds)] $LABEL skipped: owned by another machine"
    exit 0
fi
if [ "$OWNER_EXIT" -ne 0 ]; then
    echo "[$(date -Iseconds)] ERROR: routine owner check failed: $OWNER_RESULT" >&2
    exit 2
fi

case "$SCRIPT_ABS" in
    *.py)
        if ! command -v uv >/dev/null 2>&1; then
            echo "ERROR: uv not found on PATH" >&2
            exit 2
        fi
        JOB_CMD=(uv run --quiet "$SCRIPT_ABS" "$@")
        ;;
    *.sh) JOB_CMD=(/bin/bash "$SCRIPT_ABS" "$@") ;;
    *) echo "ERROR: vault script must be .py or .sh: $SCRIPT_REL" >&2; exit 2 ;;
esac

echo "[$(date -Iseconds)] $LABEL started: $SCRIPT_REL $*"
STARTED_AT=$(date +%s)
cd "$(dirname "$SCRIPT_ABS")"
JOB_EXIT=0
/usr/bin/caffeinate -i \
    "$ATELIER_PYTHON" "$SCRIPTS_DIR/command_timeout.py" \
    --seconds "${ATELIER_VAULT_JOB_TIMEOUT_SECONDS:-900}" \
    -- \
    "${JOB_CMD[@]}" || JOB_EXIT=$?
DURATION=$(( $(date +%s) - STARTED_AT ))
if [ "$JOB_EXIT" -ne 0 ]; then
    echo "[$(date -Iseconds)] $LABEL failed: exit=$JOB_EXIT duration=${DURATION}s" >&2
    exit "$JOB_EXIT"
fi
echo "[$(date -Iseconds)] $LABEL completed: duration=${DURATION}s"
