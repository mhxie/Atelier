#!/bin/bash
# routine_runner.sh — Wrapper for scheduled local routines.
#
# Invoked by launchd. Handles:
#   1. Environment setup ($OV, PATH)
#   2. Single-owner eligibility check (non-owner machines exit before work)
#   3. Claim schedule gate for completed, fenced, or not-yet-due cycles
#   4. Hostname-based stagger (0-120s) to reduce race probability
#   5. Atomic owner claim reservation or DynamoDB acquire
#   6. Local claim detail write ($OV/_meta/routine_runs/<routine>/<cycle>.toml)
#   7. Machine concurrency gate (one routine model run per host)
#   8. Headless execution through Codex; on a non-timeout Codex failure a
#      profile-declared fallback re-executes the cycle through Claude Code
#   9. Lock release + claim file update
#
# Usage:
#   routine_runner.sh <routine-name> <command>
#   routine_runner.sh autoevo-nightly /autoevo-nightly
#   routine_runner.sh <name> "/run-routine <name>"
#
# Environment:
#   OV                       — vault root (required)
#   ATELIER_SKIP_LOCK_TOUCH  — set by this script; prevents session hooks
#                               from touching the session-active lock
#   ATELIER_COORDINATION     — override coordination mode (default: reads
#                               from routine_watch.toml). Cannot downgrade the
#                               shared "owner" fence to "none".
#   ATELIER_SKIP_STAGGER     — set to 1 to skip the hostname stagger (for
#                               manual test runs via launchctl start)
#   ATELIER_SKIP_CAFFEINATE  - set to 1 to disable the macOS wake assertion
#   ATELIER_RUN_MUTEX_WAIT_SECONDS
#                            - how long to wait for another routine on this
#                               machine to finish before deferring the cycle
#                               (default: 7200; 0 disables the gate)
#   ATELIER_RUN_MUTEX_PATH   - override the machine concurrency mutex path
#   ATELIER_PREFLIGHT_TIMEOUT_SECONDS
#                            - hard timeout for each ownership/config probe
#                               (default: 30)
#   ATELIER_ROUTINE_CYCLE    - internal sanitized model environment value;
#                               always derived from the validated selected cycle
#   ATELIER_FALLBACK_CLAUDE_MODEL - optional model pin for the Claude fallback
# Unattended local routines start in Codex. `ATELIER_RUNTIME` and the local
# runtime preference apply only to interactive Atelier launchers; the only way
# Claude runs a routine is the per-profile `fallback_runtime` after a Codex
# failure (see routine_fallback.py for what counts).

set -euo pipefail

ROUTINE="${1:?Usage: routine_runner.sh <routine-name> <command>}"
COMMAND="${2:?Usage: routine_runner.sh <routine-name> <command>}"
if [[ ! "$ROUTINE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "ERROR: invalid routine name: $ROUTINE" >&2
    exit 2
fi
if [[ ! "$COMMAND" =~ ^/[A-Za-z0-9][A-Za-z0-9._-]*([[:space:]][A-Za-z0-9][A-Za-z0-9._-]*)?$ ]]; then
    echo "ERROR: scheduled commands must use /<command> with at most one safe argument: $COMMAND" >&2
    exit 2
fi
CYCLE="$(date +%Y-%m-%d)"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
ATELIER_DIR="$(dirname "$SCRIPTS_DIR")"

# --- environment setup ---------------------------------------------------

export ATELIER_SKIP_LOCK_TOUCH=1

# Source profile files in a subshell-safe way. `set -u` in the main script
# would abort on unset variables inside .zprofile/.profile, so we temporarily
# relax strictness. Only OV and PATH matter; everything else is noise.
set +eu
source "$HOME/.zprofile" 2>/dev/null || true
source "$HOME/.profile" 2>/dev/null || true
source "$ATELIER_DIR/harness/env.local.sh" 2>/dev/null || true
set -eu

# Runtime installers may put their CLI in ~/.local/bin, which only ~/.zshrc
# (interactive-only) adds to PATH, not the login profiles sourced above.
# Prepend it so launchd sees the same executable as an interactive shell.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) [ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH" ;;
esac

: "${OV:?ERROR: OV not set — export it from ~/.zprofile, ~/.profile, or ~/atelier/harness/env.local.sh}"

# Resolve calendar, wake, login, and reload invocations through one
# deterministic selector.
#
# This runs for every routine, which is what makes the declared cron the
# schedule and the plist merely how often we check. Before it, only
# autoevo-nightly consulted the selector and every other routine took
# `date +%Y-%m-%d` as its cycle, so the plist *was* the schedule: giving a
# weekly routine an hourly plist would have run it daily. The selector now
# answers "which cycle does this invocation owe", and `schedule_decision`
# below still owns whether to act on it.
#
# autoevo-nightly keeps its own pre-05:00 rule inside the selector; every other
# routine is gated on the cron declared in routine_watch.toml, falling open to
# today when none is declared or it cannot be evaluated.
if ! CYCLE_SELECTION=$(python3 "$SCRIPTS_DIR/routine_claim.py" "$ROUTINE" --select-cycle); then
    echo "ERROR: cannot select scheduled cycle for $ROUTINE" >&2
    exit 2
fi
CYCLE_ACTION=$(printf '%s' "$CYCLE_SELECTION" | python3 -c '
import json, sys
value = json.load(sys.stdin)
action = value.get("action")
if action not in {"run", "skip"}:
    raise SystemExit("cycle selection omitted a valid action")
print(action)
')
CYCLE=$(printf '%s' "$CYCLE_SELECTION" | python3 -c '
import json, sys
value = json.load(sys.stdin)
cycle = value.get("cycle_id")
if not isinstance(cycle, str) or not cycle:
    raise SystemExit("cycle selection omitted cycle_id")
print(cycle)
')
if [ "$CYCLE_ACTION" = "skip" ]; then
    echo "[$(date -Iseconds)] skipping scheduled cycle: $CYCLE_SELECTION"
    exit 0
fi
echo "[$(date -Iseconds)] scheduled cycle selected: $CYCLE_SELECTION"
if ! CYCLE=$(python3 "$SCRIPTS_DIR/routine_claim.py" "$ROUTINE" \
    --validate-cycle "$CYCLE"); then
    echo "ERROR: scheduled cycle is not a valid calendar date" >&2
    exit 2
fi

PREFLIGHT_TIMEOUT_SECONDS="${ATELIER_PREFLIGHT_TIMEOUT_SECONDS:-30}"
TIMEOUT_CMD=(python3 "$SCRIPTS_DIR/command_timeout.py" --seconds "$PREFLIGHT_TIMEOUT_SECONDS" --)

# --- single-owner gate ---------------------------------------------------
#
# This runs before claim creation and the stagger. A
# non-owner machine with an installed plist exits cleanly without touching
# shared run state. The check is repeated by routine_lock.py at acquire time so
# an ownership transfer racing this invocation still fails closed.

OWNER_RESULT=$("${TIMEOUT_CMD[@]}" python3 "$SCRIPTS_DIR/routine_owner.py" check --json 2>&1) || OWNER_EXIT=$?
OWNER_EXIT=${OWNER_EXIT:-0}
if [ "$OWNER_EXIT" -eq 1 ]; then
    echo "[$(date -Iseconds)] skipping: local routines owned by another machine ($OWNER_RESULT)"
    exit 0
fi
if [ "$OWNER_EXIT" -ne 0 ]; then
    echo "[$(date -Iseconds)] ERROR: routine owner check failed: $OWNER_RESULT" >&2
    exit 2
fi
if ! OWNER_MODE=$(printf '%s' "$OWNER_RESULT" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("coordination", ""))'); then
    echo "ERROR: owner check returned invalid JSON" >&2
    exit 2
fi
OWNER_GENERATION=$(printf '%s' "$OWNER_RESULT" | python3 -c 'import json, sys; value=json.load(sys.stdin).get("generation"); print(value if isinstance(value, int) else "")')
OWNER_GENERATION=${OWNER_GENERATION:-0}

RUNTIME="codex"

mkdir -p "$OV/cache" "$OV/_meta/routine_runs/$ROUTINE" "$OV/_meta/routine_failures/$ROUTINE"

CLAIM_DIR="$OV/_meta/routine_runs/$ROUTINE"
CLAIM_FILE="$CLAIM_DIR/$CYCLE.toml"
HOSTNAME="$(hostname)"
FAILURE_DIR="$OV/_meta/routine_failures/$ROUTINE"
AUTOEVO_EVENT_LOG=""
AUTOEVO_EVENT_LOG_REL=""

runner_event() {
    local line
    line="[$(date -Iseconds)] $*"
    printf '%s\n' "$line"
    if [ -n "$AUTOEVO_EVENT_LOG" ]; then
        printf '%s\n' "$line" >> "$AUTOEVO_EVENT_LOG"
    fi
}

# Hourly launchd checks are intentionally cheap after a cycle completes or
# while a deferred retry is cooling down. This gate runs after the owner check
# but before capability probes, stagger, lock acquisition, or model work.
if ! CLAIM_SCHEDULE_DECISION=$(python3 "$SCRIPTS_DIR/routine_claim.py" "$ROUTINE" \
    --cycle "$CYCLE" --schedule-decision); then
    echo "ERROR: cannot inspect canonical cycle claim: $CLAIM_FILE" >&2
    exit 2
fi
CLAIM_SCHEDULE_ACTION=$(printf '%s' "$CLAIM_SCHEDULE_DECISION" | python3 -c '
import json, sys
value = json.load(sys.stdin)
action = value.get("action")
if action not in {"run", "skip"}:
    raise SystemExit("claim schedule decision omitted a valid action")
print(action)
')
if [ "$CLAIM_SCHEDULE_ACTION" = "skip" ]; then
    echo "[$(date -Iseconds)] skipping scheduled check: $CLAIM_SCHEDULE_DECISION"
    exit 0
fi

write_failure_diagnostic() {
    local phase="$1"
    local error_json="$2"
    local diagnostic_file="$FAILURE_DIR/$(date +%Y%m%dT%H%M%S)-$HOSTNAME-$$.toml"
    cat > "$diagnostic_file" <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
recorded_at = "$(date -Iseconds)"
phase = "$phase"
status = "failed"
error = $error_json
EOF
    echo "[$(date -Iseconds)] diagnostic: $diagnostic_file" >&2
}

write_claim() {
    python3 "$SCRIPTS_DIR/routine_claim.py" "$ROUTINE" --cycle "$CYCLE" >/dev/null
}

# Routine prompts run repo scripts, which need tomllib (3.11+). Inside the
# sandbox a bare `python3` resolved to macOS's 3.9, so prompts use
# $ATELIER_PYTHON instead of trusting PATH ordering.
if ! ATELIER_PYTHON=$("$SCRIPTS_DIR/find_python.sh"); then
    write_failure_diagnostic "python-resolution" '"no-python3.11-or-newer"'
    echo "ERROR: no python3 >= 3.11 on PATH" >&2
    exit 2
fi

# Resolve the private routine's declared support surface against the public
# capability profiles before claiming a cycle. This fails closed on missing
# CLIs, required Codex plugins, owner drift, or an unloaded launchd job.
if ! PROFILE_RECORD=$("${TIMEOUT_CMD[@]}" python3 "$SCRIPTS_DIR/routine_audit.py" resolve "$ROUTINE" \
    --surface local --check-system --runtime "$RUNTIME" --command "$COMMAND" --format tsv 2>&1); then
    SAFE_PROFILE_ERROR=$(printf '%s' "$PROFILE_RECORD" | python3 -c 'import json, sys; print(json.dumps("routine-preflight-failed: " + sys.stdin.read().replace("\n", " ")))')
    write_failure_diagnostic "capability-preflight" "$SAFE_PROFILE_ERROR"
    echo "ERROR: routine capability preflight failed: $PROFILE_RECORD" >&2
    exit 2
fi

IFS=$'\t' read -r ROUTINE_PROFILE CODEX_SANDBOX ATELIER_ACCESS_MODE WEB_SEARCH_MODE SHELL_NETWORK_MODE USER_CONFIG_MODE ROUTINE_TIMEOUT_SECONDS REASONING_EFFORT PROFILE_FINGERPRINT PERMISSION_ALLOWLIST FALLBACK_RUNTIME <<< "$PROFILE_RECORD"
if [ -z "$ROUTINE_PROFILE" ] || [ -z "$CODEX_SANDBOX" ] || [ -z "$ATELIER_ACCESS_MODE" ] || [ -z "$WEB_SEARCH_MODE" ] || [ -z "$SHELL_NETWORK_MODE" ] || [ -z "$USER_CONFIG_MODE" ] || [ -z "$ROUTINE_TIMEOUT_SECONDS" ] || [ -z "$REASONING_EFFORT" ] || [ -z "$PROFILE_FINGERPRINT" ] || [ -z "$PERMISSION_ALLOWLIST" ] || [ -z "$FALLBACK_RUNTIME" ]; then
    echo "ERROR: routine capability preflight returned an incomplete profile" >&2
    exit 2
fi
echo "[$(date -Iseconds)] preflight: profile=$ROUTINE_PROFILE sandbox=$CODEX_SANDBOX atelier_access=$ATELIER_ACCESS_MODE web=$WEB_SEARCH_MODE shell_network=$SHELL_NETWORK_MODE user_config=$USER_CONFIG_MODE permissions=$PERMISSION_ALLOWLIST timeout=${ROUTINE_TIMEOUT_SECONDS}s reasoning=$REASONING_EFFORT fallback=$FALLBACK_RUNTIME"

CAFFEINATE_PID=""
# Held from the machine concurrency gate until the EXIT trap. Declared here so
# every exit path between the two can release it.
RUN_MUTEX_DIR=""
if [ "${ATELIER_SKIP_CAFFEINATE:-0}" != "1" ]; then
    if ! command -v caffeinate >/dev/null 2>&1; then
        write_failure_diagnostic "wake-assertion" '"caffeinate-not-found"'
        echo "ERROR: caffeinate is required for local routine execution." >&2
        exit 2
    fi
    caffeinate -i -w "$$" >/dev/null 2>&1 &
    CAFFEINATE_PID=$!
    echo "[$(date -Iseconds)] wake assertion: active"
fi

# --- stagger (hostname-based, 0-120s) ------------------------------------

if [ "${ATELIER_SKIP_STAGGER:-0}" != "1" ]; then
    HASH=$(echo -n "$(hostname)" | cksum | awk '{print $1}')
    DELAY=$((HASH % 120))
    echo "[$(date -Iseconds)] stagger: sleeping ${DELAY}s (hostname=$(hostname))"
    sleep "$DELAY"
fi

# --- DynamoDB lock --------------------------------------------------------
# Credentials come from a dedicated non-interactive AWS profile that boto3
# reads straight from ~/.aws/credentials. No aws-vault, no macOS Keychain:
# the Keychain is locked when the screen is locked at 05:00, which is what
# silently broke earlier runs. The profile is scoped to DynamoDB
# {Get,Put,Update}Item on the lock table only. One-time setup lives in
# scripts/launchd/README.md § Step 2.

LOCK_CMD=(uv run --directory "$ATELIER_DIR" python3 "$SCRIPTS_DIR/routine_lock.py")
LOCK_WITH_TIMEOUT=("${TIMEOUT_CMD[@]}" "${LOCK_CMD[@]}")

release_lock() {
    if ! RELEASE_RESULT=$("${LOCK_WITH_TIMEOUT[@]}" release "$ROUTINE" --cycle "$CYCLE" 2>&1); then
        return 1
    fi
    printf '%s' "$RELEASE_RESULT" | python3 -c '
import json, sys
value = json.load(sys.stdin)
valid = (
    value.get("released") is True
    and value.get("coordination") == sys.argv[2]
    and value.get("cycle") == sys.argv[1]
)
raise SystemExit(0 if valid else 1)
' "$CYCLE" "$COORD_MODE"
}

# Read the backend without opening a DynamoDB client so the dedicated AWS
# profile can be set before the first network operation.
COORD_MODE=$("${LOCK_WITH_TIMEOUT[@]}" backend | python3 -c "import sys,json; print(json.load(sys.stdin).get('coordination',''))")

if [ "$COORD_MODE" = "dynamodb" ]; then
    # boto3 resolves this profile from ~/.aws/credentials with zero prompts.
    export AWS_PROFILE="${ATELIER_LOCK_AWS_PROFILE:-atelier-lock}"
fi

LOCK_RESULT=$("${LOCK_WITH_TIMEOUT[@]}" acquire "$ROUTINE" --cycle "$CYCLE" 2>&1) || LOCK_EXIT=$?
LOCK_EXIT=${LOCK_EXIT:-0}

echo "[$(date -Iseconds)] lock acquire: exit=$LOCK_EXIT result=$LOCK_RESULT"

if [ "$LOCK_EXIT" -eq 1 ]; then
    if ! printf '%s' "$LOCK_RESULT" | python3 -c '
import json, sys
value = json.load(sys.stdin)
valid = (
    value.get("acquired") is False
    and value.get("coordination") == sys.argv[2]
    and value.get("cycle") == sys.argv[1]
)
raise SystemExit(0 if valid else 1)
' "$CYCLE" "$COORD_MODE"; then
        SAFE_RESULT=$(printf '%s' "$LOCK_RESULT" | python3 -c 'import json, sys; print(json.dumps("invalid-lock-contention-result: " + sys.stdin.read().replace("\n", " ")))')
        write_failure_diagnostic "lock-acquire" "$SAFE_RESULT"
        echo "[$(date -Iseconds)] ERROR: lock acquire exited 1 without a valid contention result" >&2
        exit 2
    fi
    # Genuine contention: another machine owns this cycle and will write the
    # shared output plus its own claim under $OV. Stand down cleanly; do NOT
    # write a claim here (the holder's claim covers the session cue check).
    echo "[$(date -Iseconds)] skipping: lock held by another machine"
    exit 0
fi

if [ "$LOCK_EXIT" -ne 0 ]; then
    # 2 = credential / DynamoDB failure; anything else (127 = uv missing from
    # the launchd PATH, etc.) is equally unknown lock state — fail CLOSED, not
    # open. Record a machine-specific diagnostic instead of touching the
    # canonical cycle claim, which may belong to another machine.
    SAFE_RESULT=$(printf '%s' "$LOCK_RESULT" | python3 -c 'import json, sys; print(json.dumps("lock-acquire-failed: " + sys.stdin.read().replace("\n", " ")))')
    write_failure_diagnostic "lock-acquire" "$SAFE_RESULT"
    echo "[$(date -Iseconds)] ERROR: lock acquire failed (credentials or DynamoDB). Fix before retrying." >&2
    exit 2
fi

if ! LOCK_METADATA=$(printf '%s' "$LOCK_RESULT" | python3 -c '
import json, sys
value = json.load(sys.stdin)
if (
    value.get("acquired") is not True
    or value.get("coordination") != sys.argv[2]
    or value.get("cycle") != sys.argv[1]
):
    raise SystemExit(1)
print("true" if value.get("retry_authorized") is True else "false")
print("true" if value.get("claim_reserved") is True else "false")
' "$CYCLE" "$COORD_MODE"); then
    SAFE_RESULT=$(printf '%s' "$LOCK_RESULT" | python3 -c 'import json, sys; print(json.dumps("invalid-lock-success-result: " + sys.stdin.read().replace("\n", " ")))')
    write_failure_diagnostic "lock-acquire" "$SAFE_RESULT"
    echo "[$(date -Iseconds)] ERROR: lock acquire succeeded without valid JSON attestation" >&2
    exit 2
fi
LOCK_RETRY_AUTHORIZED=$(printf '%s\n' "$LOCK_METADATA" | sed -n '1p')
LOCK_CLAIM_RESERVED=$(printf '%s\n' "$LOCK_METADATA" | sed -n '2p')

if [ "$COORD_MODE" != "owner" ] && [ "$LOCK_CLAIM_RESERVED" != "true" ] && [ "$LOCK_RETRY_AUTHORIZED" != "true" ] && [ -f "$CLAIM_FILE" ]; then
    if ! EXISTING_STATUS=$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get("status", ""))' "$CLAIM_FILE"); then
        write_failure_diagnostic "claim-read" '"canonical-claim-is-not-valid-toml"'
        echo "[$(date -Iseconds)] ERROR: canonical claim is not valid TOML; lock retained" >&2
        exit 2
    fi
    case "$EXISTING_STATUS" in
        running|completed|failed|completion-uncertain)
            echo "[$(date -Iseconds)] skipping: canonical cycle claim is already $EXISTING_STATUS"
            if [ "$COORD_MODE" = "dynamodb" ] && [ "$EXISTING_STATUS" != "completed" ]; then
                echo "[$(date -Iseconds)] lock retained pending explicit cycle recovery"
                exit 0
            fi
            if ! release_lock; then
                echo "ERROR: could not release lock after duplicate-claim skip" >&2
                exit 2
            fi
            exit 0
            ;;
        deferred|retry-approved)
            ;;
        *)
            write_failure_diagnostic "claim-read" '"unknown-canonical-claim-status"'
            echo "[$(date -Iseconds)] ERROR: canonical claim has an unknown status; lock retained" >&2
            exit 2
            ;;
    esac
fi
if [ "$COORD_MODE" = "owner" ]; then
    LOCK_GENERATION=$(printf '%s' "$LOCK_RESULT" | python3 -c 'import json, sys; value=json.load(sys.stdin).get("generation"); print(value if isinstance(value, int) else "")')
    if [ -z "$LOCK_GENERATION" ]; then
        echo "ERROR: owner lock acquisition omitted its generation" >&2
        exit 2
    fi
    OWNER_GENERATION="$LOCK_GENERATION"
fi

# --- write local claim file -----------------------------------------------

CLAIMED_AT="$(date -Iseconds)"
CLAIM_EVENT_FIELD=""
if [ "$ROUTINE" = "autoevo-nightly" ]; then
    AUTOEVO_EVENT_LOG=$(mktemp "$OV/cache/autoevo-runner-${CYCLE}.log.XXXXXX")
    AUTOEVO_EVENT_LOG_REL="${AUTOEVO_EVENT_LOG#$OV/}"
    AUTOEVO_EVENT_LOG_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$AUTOEVO_EVENT_LOG_REL")
    CLAIM_EVENT_FIELD=$(printf 'event_log = %s' "$AUTOEVO_EVENT_LOG_TOML")
fi

write_claim <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
contract_version = 2
profile = "$ROUTINE_PROFILE"
profile_fingerprint = "$PROFILE_FINGERPRINT"
runtime = "$RUNTIME"
atelier_access = "$ATELIER_ACCESS_MODE"
shell_network = "$SHELL_NETWORK_MODE"
owner_generation = $OWNER_GENERATION
retry_authorized = $LOCK_RETRY_AUTHORIZED
claimed_at = "$CLAIMED_AT"
status = "running"
$CLAIM_EVENT_FIELD
EOF

runner_event "claimed: $CLAIM_FILE"

# A shell-level failure (including `set -u` or an unexpected helper exit) can
# otherwise bypass the normal completion block and leave a false `running`
# claim forever. Mark it failed on any nonzero exit after claim creation.
RUN_FINALIZED=0
ROUTINE_CWD=""
ROUTINE_RESULT_FILE=""
# Populated only when the primary runtime failed and a declared fallback ran;
# appended to the final claim so the record names both runtimes.
FALLBACK_FIELDS=""
finalize_unexpected_exit() {
    local exit_code=$?
    if [ "$RUN_FINALIZED" = "0" ] && [ "$exit_code" -ne 0 ]; then
        set +e
        write_claim <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
contract_version = 2
profile = "$ROUTINE_PROFILE"
profile_fingerprint = "$PROFILE_FINGERPRINT"
runtime = "$RUNTIME"
atelier_access = "$ATELIER_ACCESS_MODE"
shell_network = "$SHELL_NETWORK_MODE"
owner_generation = $OWNER_GENERATION
retry_authorized = $LOCK_RETRY_AUTHORIZED
claimed_at = "$CLAIMED_AT"
status = "failed"
completed_at = "$(date -Iseconds)"
error = "runner-exited-unexpectedly"
exit_code = $exit_code
$CLAIM_EVENT_FIELD
$FALLBACK_FIELDS
EOF
        echo "[$(date -Iseconds)] ERROR: unexpected runner exit=$exit_code; claim marked failed" >&2
    fi
    if [ -n "$ROUTINE_CWD" ]; then
        python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$ROUTINE_CWD"
    fi
    if [ -n "$ROUTINE_RESULT_FILE" ] && [ -f "$ROUTINE_RESULT_FILE" ]; then
        python3 -c 'from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)' "$ROUTINE_RESULT_FILE"
    fi
    if [ -n "$CAFFEINATE_PID" ]; then
        kill "$CAFFEINATE_PID" 2>/dev/null || true
        wait "$CAFFEINATE_PID" 2>/dev/null || true
    fi
    if [ -n "$RUN_MUTEX_DIR" ]; then
        "$SCRIPTS_DIR/routine_run_mutex.sh" release "$$" "$RUN_MUTEX_DIR" || true
        RUN_MUTEX_DIR=""
    fi
}
trap finalize_unexpected_exit EXIT

# A cooperative transfer should not proceed while this running claim is
# synchronized. Recheck the generation immediately before model execution to
# fence a transfer already visible on this machine.
if [ "$COORD_MODE" = "owner" ]; then
    CURRENT_OWNER_RESULT=$("${TIMEOUT_CMD[@]}" python3 "$SCRIPTS_DIR/routine_owner.py" check --json)
    CURRENT_OWNER_GENERATION=$(printf '%s' "$CURRENT_OWNER_RESULT" | python3 -c 'import json, sys; value=json.load(sys.stdin).get("generation"); print(value if isinstance(value, int) else "")')
    if [ -z "$CURRENT_OWNER_GENERATION" ] || [ "$CURRENT_OWNER_GENERATION" != "$OWNER_GENERATION" ]; then
        echo "ERROR: local routine ownership changed before execution" >&2
        exit 2
    fi
fi

# Autoevo's safety gates are deterministic and must complete before the model
# is started. This fast path produces the same canonical audit artifact and a
# validated noop result, then leaves the claim in `deferred` so a later
# calendar trigger or RunAtLoad catch-up can retry without operator recovery.
# The command repeats the gates after model launch as defense in depth.
if [ "$ROUTINE" = "autoevo-nightly" ] && [ "${DRY_RUN:-0}" != "1" ]; then
    FAST_PREFLIGHT_STARTED_AT=$(date +%s)
    FAST_RESULT_FILE=$(mktemp "${TMPDIR:-/tmp}/atelier-autoevo-preflight-result.XXXXXX")
    ROUTINE_RESULT_FILE="$FAST_RESULT_FILE"
    if ! FAST_PREFLIGHT_JSON=$(python3 "$SCRIPTS_DIR/autoevo_preflight.py" \
        --record-blocker \
        --result-file "$FAST_RESULT_FILE" \
        --run-date "$CYCLE" \
        --cycle "$CYCLE" \
        --json); then
        echo "ERROR: deterministic autoevo preflight failed: $FAST_PREFLIGHT_JSON" >&2
        exit 2
    fi
    FAST_PREFLIGHT_READY=$(printf '%s' "$FAST_PREFLIGHT_JSON" | python3 -c '
import json, sys
value = json.load(sys.stdin).get("ready")
if value is True:
    print("true")
elif value is False:
    print("false")
else:
    raise SystemExit("preflight JSON omitted boolean ready")
')
    if [ "$FAST_PREFLIGHT_READY" = "false" ]; then
        FAST_GATE=$(printf '%s' "$FAST_PREFLIGHT_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("gate", "unknown"))')
        FAST_AUDIT_COMMIT=$(printf '%s' "$FAST_PREFLIGHT_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("audit_commit", "unknown"))')
        FAST_RETRY_AFTER_EPOCH=$(printf '%s' "$FAST_PREFLIGHT_JSON" | python3 -c '
import json, sys
value = json.load(sys.stdin).get("retry_after_epoch")
if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise SystemExit("preflight JSON omitted a valid retry_after_epoch")
print(value)
')
        if [ "$FAST_AUDIT_COMMIT" = "reused" ]; then
            RUN_OUTCOME="noop"
            RUN_OUTPUT_FILE=$(printf '%s' "$FAST_PREFLIGHT_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin)["output_file"])')
        else
            if ! RESULT_ATTESTATION=$(python3 "$SCRIPTS_DIR/routine_result.py" "$ROUTINE" \
                --cycle "$CYCLE" \
                --claimed-at "$CLAIMED_AT" \
                --result-file "$FAST_RESULT_FILE" 2>&1); then
                echo "ERROR: deterministic preflight audit failed delivery attestation: $RESULT_ATTESTATION" >&2
                exit 2
            fi
            RUN_OUTCOME=$(printf '%s' "$RESULT_ATTESTATION" | python3 -c 'import json, sys; print(json.load(sys.stdin)["outcome"])')
            RUN_OUTPUT_FILE=$(printf '%s' "$RESULT_ATTESTATION" | python3 -c 'import json, sys; print(json.load(sys.stdin)["output_file"])')
        fi
        FAST_PREFLIGHT_ENDED_AT=$(date +%s)
        FAST_PREFLIGHT_DURATION=$(( FAST_PREFLIGHT_ENDED_AT - FAST_PREFLIGHT_STARTED_AT ))
        if ! release_lock; then
            echo "ERROR: deterministic preflight completed but lock release failed: $RELEASE_RESULT" >&2
            exit 2
        fi
        FAST_GATE_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$FAST_GATE")
        RUN_OUTCOME_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$RUN_OUTCOME")
        RUN_OUTPUT_FILE_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$RUN_OUTPUT_FILE")
        write_claim <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
contract_version = 2
profile = "$ROUTINE_PROFILE"
profile_fingerprint = "$PROFILE_FINGERPRINT"
runtime = "$RUNTIME"
atelier_access = "$ATELIER_ACCESS_MODE"
shell_network = "$SHELL_NETWORK_MODE"
owner_generation = $OWNER_GENERATION
retry_authorized = $LOCK_RETRY_AUTHORIZED
claimed_at = "$CLAIMED_AT"
status = "deferred"
deferred_at = "$(date -Iseconds)"
duration_seconds = $FAST_PREFLIGHT_DURATION
outcome = $RUN_OUTCOME_TOML
output_file = $RUN_OUTPUT_FILE_TOML
blocker = $FAST_GATE_TOML
retry_scheduled = true
retry_after_epoch = $FAST_RETRY_AFTER_EPOCH
$CLAIM_EVENT_FIELD
EOF
        RUN_FINALIZED=1
        runner_event "autoevo deferred before model launch: blocker=$FAST_GATE duration=${FAST_PREFLIGHT_DURATION}s"
        runner_event "delivery validated: outcome=$RUN_OUTCOME output=$RUN_OUTPUT_FILE"
        exit 0
    fi
    python3 -c 'from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)' "$FAST_RESULT_FILE"
    ROUTINE_RESULT_FILE=""
    runner_event "deterministic autoevo preflight passed"
fi

# --- host readiness gate --------------------------------------------------
# Every exit-124 timeout recorded since the profile budgets landed (2026-07-26)
# started late relative to that routine's normal trigger, i.e. on a launchd
# catch-up after the host had been asleep. The same routines finish in 90-680s
# when they fire on time; the late ones stall a few hundred transcript lines in,
# emit nothing further, and are killed at the full budget with no artifact. One
# routine burned 14 hours that way across 14 cycles.
#
# A model launched before the network and the vault mount are back does not
# fail, it blocks, and the budget only decides how long the waste lasts. So wait
# briefly for the host to actually be ready, and defer the cycle if it is not.
# A deferred claim is retried by the next calendar trigger or RunAtLoad
# catch-up, which is what the hourly recovery path already expects.

READINESS_TIMEOUT_SECONDS="${ATELIER_READINESS_TIMEOUT_SECONDS:-120}"
READINESS_URL="${ATELIER_READINESS_URL:-https://api.openai.com/v1/models}"
READINESS_BLOCKER=""
if [ "${DRY_RUN:-0}" != "1" ] && [ "$READINESS_TIMEOUT_SECONDS" -gt 0 ]; then
    READINESS_STARTED_AT=$(date +%s)
    READINESS_DEADLINE=$(( READINESS_STARTED_AT + READINESS_TIMEOUT_SECONDS ))
    while :; do
        READINESS_BLOCKER=""
        # Listing the vault forces the cloud file provider to materialize it;
        # a bare `-d` test passes against a mount that later blocks on read.
        if ! "${TIMEOUT_CMD[@]}" /bin/ls "$OV/_meta" >/dev/null 2>&1; then
            READINESS_BLOCKER="vault-unreadable"
        elif ! curl -sS --max-time 5 -o /dev/null "$READINESS_URL" >/dev/null 2>&1; then
            READINESS_BLOCKER="network-unreachable"
        fi
        [ -z "$READINESS_BLOCKER" ] && break
        [ "$(date +%s)" -ge "$READINESS_DEADLINE" ] && break
        sleep 5
    done
    READINESS_DURATION=$(( $(date +%s) - READINESS_STARTED_AT ))
    if [ -n "$READINESS_BLOCKER" ]; then
        if ! release_lock; then
            echo "ERROR: readiness gate deferred but lock release failed: $RELEASE_RESULT" >&2
            exit 2
        fi
        READINESS_BLOCKER_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$READINESS_BLOCKER")
        write_claim <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
contract_version = 2
profile = "$ROUTINE_PROFILE"
profile_fingerprint = "$PROFILE_FINGERPRINT"
runtime = "$RUNTIME"
atelier_access = "$ATELIER_ACCESS_MODE"
shell_network = "$SHELL_NETWORK_MODE"
owner_generation = $OWNER_GENERATION
retry_authorized = $LOCK_RETRY_AUTHORIZED
claimed_at = "$CLAIMED_AT"
status = "deferred"
deferred_at = "$(date -Iseconds)"
duration_seconds = $READINESS_DURATION
blocker = $READINESS_BLOCKER_TOML
retry_scheduled = true
$CLAIM_EVENT_FIELD
EOF
        runner_event "deferred before model launch: host not ready (blocker=$READINESS_BLOCKER after ${READINESS_DURATION}s)"
        exit 0
    fi
    if [ "$READINESS_DURATION" -gt 0 ]; then
        runner_event "readiness gate: host became ready after ${READINESS_DURATION}s"
    fi
fi

# --- machine concurrency gate ---------------------------------------------
# One routine model run per host. The reasoning, the mutex primitive, and the
# stale-holder reaper live in routine_run_mutex.sh; this block owns only the
# runner-side consequence, which is to defer the cycle when the wait expires.
#
# The wait is long on purpose, and deliberately longer than any holder can
# legitimately need. A waiter has not launched its model yet, so queueing costs
# it nothing from its own timeout budget; a defer, by contrast, costs a whole
# cycle, because `schedule_decision` treats a failed claim as terminal and only
# `deferred` and `retry-approved` are ever re-attempted.
#
# Sized against the largest profile budget (local-maintenance, 7200s), which is
# the ceiling on how long any holder can run before command_timeout.py kills
# it. Measured against real work the ceiling is generous: since the profile
# budgets landed on 2026-07-26 the longest successful run on this fleet is
# 1779s and the median is 151s. So in practice the queue drains in minutes and
# the defer fires only for a holder that is genuinely wedged.

RUN_MUTEX_WAIT_SECONDS="${ATELIER_RUN_MUTEX_WAIT_SECONDS:-7200}"
RUN_MUTEX_PATH="${ATELIER_RUN_MUTEX_PATH:-$HOME/Library/Caches/com.atelier/routine-run.lock}"
RUN_MUTEX_HOLDER=""
if [ "${DRY_RUN:-0}" != "1" ] && [ "$RUN_MUTEX_WAIT_SECONDS" -gt 0 ]; then
    MUTEX_STARTED_AT=$(date +%s)
    MUTEX_EXIT=0
    RUN_MUTEX_HOLDER=$("$SCRIPTS_DIR/routine_run_mutex.sh" acquire \
        "$ROUTINE" "$$" "$RUN_MUTEX_WAIT_SECONDS" "$RUN_MUTEX_PATH") || MUTEX_EXIT=$?
    MUTEX_DURATION=$(( $(date +%s) - MUTEX_STARTED_AT ))
    if [ "$MUTEX_EXIT" -eq 0 ]; then
        RUN_MUTEX_DIR="$RUN_MUTEX_PATH"
    elif [ "$MUTEX_EXIT" -ne 1 ]; then
        write_failure_diagnostic "run-mutex" '"run-mutex-acquire-failed"'
        echo "ERROR: run mutex acquire failed with exit $MUTEX_EXIT" >&2
        exit 2
    fi
    if [ -z "$RUN_MUTEX_DIR" ]; then
        if ! release_lock; then
            echo "ERROR: concurrency gate deferred but lock release failed: $RELEASE_RESULT" >&2
            exit 2
        fi
        MUTEX_BLOCKER_TOML=$(python3 -c 'import json, sys; print(json.dumps("routine-already-running:" + sys.argv[1]))' "${RUN_MUTEX_HOLDER:-unknown}")
        write_claim <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
contract_version = 2
profile = "$ROUTINE_PROFILE"
profile_fingerprint = "$PROFILE_FINGERPRINT"
runtime = "$RUNTIME"
atelier_access = "$ATELIER_ACCESS_MODE"
shell_network = "$SHELL_NETWORK_MODE"
owner_generation = $OWNER_GENERATION
retry_authorized = $LOCK_RETRY_AUTHORIZED
claimed_at = "$CLAIMED_AT"
status = "deferred"
deferred_at = "$(date -Iseconds)"
duration_seconds = $MUTEX_DURATION
blocker = $MUTEX_BLOCKER_TOML
retry_scheduled = true
$CLAIM_EVENT_FIELD
EOF
        runner_event "deferred before model launch: ${RUN_MUTEX_HOLDER:-another routine} still running after ${MUTEX_DURATION}s"
        exit 0
    fi
    if [ "$MUTEX_DURATION" -gt 0 ]; then
        runner_event "run mutex: acquired after queueing ${MUTEX_DURATION}s behind another routine"
    fi
fi

# --- execute routine ------------------------------------------------------

# Neither runtime exposes bot-only commands as user skills. Resolve the command
# through the portable registry, then give the headless CLI a bounded adapter
# prompt that tells it to read and execute the authoritative command source.
# The resolution and the prompt are shared by run_codex and run_claude so the
# fallback executes exactly the contract the primary was given.
COMMAND_SOURCE=""
COMMAND_HINT=""
ADAPTER_PROMPT=""
resolve_routine_command() {
    local command_expr command_name command_arg command_record prompt_file

    command_expr="${COMMAND#/}"
    command_name="${command_expr%% *}"
    command_arg=""
    if [[ "$command_expr" == *" "* ]]; then
        command_arg="${command_expr#* }"
    fi
    if [ "$command_expr" = "$COMMAND" ] || [ -z "$command_name" ]; then
        echo "ERROR: Codex scheduled commands must use /<command> form: $COMMAND" >&2
        return 2
    fi

    if ! command_record=$(uv run --quiet --directory "$ATELIER_DIR" python3 -c '
import pathlib, sys, tomllib

registry_path = pathlib.Path(sys.argv[1])
command_name = sys.argv[2]
commands = tomllib.loads(registry_path.read_text()).get("commands", {})
row = commands.get(command_name)
if not isinstance(row, dict):
    raise SystemExit(f"command not registered: {command_name}")
source = row.get("source")
prompt = row.get("codex_prompt")
if not isinstance(source, str) or not isinstance(prompt, str):
    raise SystemExit(f"command missing source/codex_prompt: {command_name}")
if any(ch in source or ch in prompt for ch in ("\t", "\n")):
    raise SystemExit(f"command metadata must be single-line: {command_name}")
print(f"{source}\t{prompt}")
' "$ATELIER_DIR/harness/commands.toml" "$command_name"); then
        echo "ERROR: failed to resolve Codex command metadata: $command_name" >&2
        return 2
    fi

    IFS=$'\t' read -r COMMAND_SOURCE COMMAND_HINT <<< "$command_record"
    if [ ! -f "$ATELIER_DIR/$COMMAND_SOURCE" ]; then
        echo "ERROR: registered command source not found: $COMMAND_SOURCE" >&2
        return 2
    fi

    if [ "$command_name" = "run-routine" ]; then
        if [ -z "$command_arg" ] || [ "$command_arg" != "$ROUTINE" ]; then
            echo "ERROR: /run-routine argument must match routine name: $ROUTINE" >&2
            return 2
        fi
        prompt_file="$OV/_routine_prompts/$command_arg.md"
        if ! "${TIMEOUT_CMD[@]}" python3 "$SCRIPTS_DIR/routine_prompt_guard.py" "$prompt_file"; then
            echo "ERROR: private routine prompt failed preflight: $prompt_file" >&2
            return 2
        fi
    fi
}

# $1 names how the runtime executes the command source: Codex reads it through
# its adaptation table, Claude Code runs the same markdown natively.
render_adapter_prompt() {
    local adapt="$1"
    # Named substitution: python str.format raises KeyError on any missing
    # field, unlike bash printf which silently mis-slots on a %s/arg count
    # mismatch (13 positional args lived here until 2026-08-23).
    if ! ADAPTER_PROMPT=$(RP_HINT="$COMMAND_HINT" RP_COMMAND="$COMMAND" RP_PROFILE="$ROUTINE_PROFILE" \
        RP_SANDBOX="$CODEX_SANDBOX" RP_ACCESS="$ATELIER_ACCESS_MODE" RP_WEB="$WEB_SEARCH_MODE" \
        RP_SHELLNET="$SHELL_NETWORK_MODE" RP_USERCFG="$USER_CONFIG_MODE" RP_ALLOW="$PERMISSION_ALLOWLIST" \
        RP_DIR="$ATELIER_DIR" RP_SOURCE="$COMMAND_SOURCE" RP_ADAPT="$adapt" python3 - <<'RPY'
import os
fields = {k[3:].lower(): os.environ[k] for k in (
    "RP_HINT", "RP_COMMAND", "RP_PROFILE", "RP_SANDBOX", "RP_ACCESS", "RP_WEB",
    "RP_SHELLNET", "RP_USERCFG", "RP_ALLOW", "RP_DIR", "RP_SOURCE", "RP_ADAPT")}
print(
    "{hint}\n\nThis is an unattended local Atelier routine, not an interactive user command. "
    "Invocation: `{command}`. The wrapper has already completed owner, support, capability, dependency, "
    "launchd, and credential-guard preflight with profile `{profile}` (sandbox={sandbox}, "
    "atelier_access={access}, web={web}, shell_network={shellnet}, user_config={usercfg}). "
    "Effective action permission allowlist: `{allow}`. Treat it as a strict model-level allowlist: "
    "skip every connector, CLI, web, or filesystem action not listed, even if an optional integration "
    "is installed. This is not a shell-level connector ACL. Read `{dir}/AGENTS.md` and `{dir}/CLAUDE.md` "
    "first, then read `{dir}/{source}` completely and execute it in this process {adapt}. "
    "Treat the Atelier repository as read-only unless atelier_access is read-write. "
    "Do not re-audit routine_profiles.toml, routine_runner.sh, remote-routines.md, launchd state, or "
    "the private watch registry; trust the wrapper preflight. This is operational work, not user-facing "
    "reflection; load only files required by the command and archived prompt after the mandatory "
    "session-start reads. The scheduled invocation authorizes only the autonomous writes and commits "
    "explicitly allowed by that command contract. Do not ask for interactive input. Ignore unrelated "
    "SessionStart cues. Stop safely if the command requires authority it does not grant. The final "
    "response must contain only JSON matching the supplied schema. Set outcome to delivered only after "
    "writing the canonical output artifact, noop only for an intentional documented no-op that still "
    "writes its audit artifact, or failed if the routine stops without a valid artifact. Report the "
    "canonical artifact path in output_file for delivered and noop outcomes.".format(**fields)
)
RPY
    ); then
        echo "ERROR: failed to render the routine prompt (missing field)" >&2
        return 2
    fi
}

run_codex() {
    local codex_prompt
    local env_name
    local -a codex_env codex_global_args codex_exec_args

    if ! command -v codex >/dev/null 2>&1; then
        echo "ERROR: codex not found on PATH" >&2
        return 127
    fi
    resolve_routine_command || return $?
    render_adapter_prompt "using the Codex adaptation table" || return 2
    codex_prompt="$ADAPTER_PROMPT"

    # The resolved capability profile supplies the least-privilege sandbox,
    # web mode, and user-config policy. Maintenance work that must commit gets
    # danger-full-access; ordinary routines get workspace-write plus $OV.
    # Project hooks are trusted only for the maintenance profile rooted in the
    # Atelier checkout. Ordinary vault-rooted routines do not load repo hooks.
    # Keep the model-facing shell environment narrow. The lock step may have
    # loaded unrelated credentials from login profiles, and autoevo does not
    # need them. Preserve only runtime paths, vault routing, hook guards, and
    # optional Codex location / CA settings needed to reach cached login and
    # installed connectors.
    codex_env=(
        env -i
        "HOME=$HOME"
        "PATH=$PATH"
        "OV=$OV"
        "ZDOTDIR=$ATELIER_DIR/harness/routine-shell"
        "TMPDIR=${TMPDIR:-/tmp}"
        "LANG=${LANG:-en_US.UTF-8}"
        "ATELIER_ACTIVE_RUNTIME=codex"
        "ATELIER_PYTHON=$ATELIER_PYTHON"
        "ATELIER_ROUTINE_CYCLE=$CYCLE"
        "ATELIER_ROUTINE_PROFILE=$ROUTINE_PROFILE"
        "ATELIER_ROUTINE_PERMISSIONS=$PERMISSION_ALLOWLIST"
        "ATELIER_SKIP_LOCK_TOUCH=1"
    )
    for env_name in DRY_RUN CODEX_HOME CODEX_CA_CERTIFICATE SSL_CERT_FILE; do
        if [ -n "${!env_name:-}" ]; then
            codex_env+=("$env_name=${!env_name}")
        fi
    done

    if [ "$WEB_SEARCH_MODE" = "live" ]; then
        codex_global_args=(
            -c 'approval_policy="never"'
            -c "model_reasoning_effort=\"$REASONING_EFFORT\""
            --search
        )
    else
        codex_global_args=(
            -c 'approval_policy="never"'
            -c "model_reasoning_effort=\"$REASONING_EFFORT\""
            -c 'web_search="disabled"'
        )
    fi

    if [ "$CODEX_SANDBOX" = "workspace-write" ]; then
        if [ "$SHELL_NETWORK_MODE" = "enabled" ]; then
            codex_global_args+=(
                -c 'sandbox_workspace_write.network_access=true'
            )
        else
            codex_global_args+=(
                -c 'sandbox_workspace_write.network_access=false'
            )
        fi
    fi

    codex_exec_args=(
        --sandbox "$CODEX_SANDBOX"
        --ephemeral
        --color never
        --output-schema "$ATELIER_DIR/harness/routine_result.schema.json"
    )
    ROUTINE_RESULT_FILE=$(mktemp "${TMPDIR:-/tmp}/atelier-routine-result.XXXXXX")
    codex_exec_args+=(--output-last-message "$ROUTINE_RESULT_FILE")
    if [ "$ATELIER_ACCESS_MODE" = "read-write" ]; then
        codex_exec_args+=(--dangerously-bypass-hook-trust --add-dir "$OV" -C "$ATELIER_DIR")
    else
        ROUTINE_CWD=$(mktemp -d "${TMPDIR:-/tmp}/atelier-routine-cwd.XXXXXX")
        codex_exec_args+=(--skip-git-repo-check --add-dir "$OV" -C "$ROUTINE_CWD")
    fi
    if [ "$USER_CONFIG_MODE" = "ignore" ]; then
        codex_exec_args=(--ignore-user-config "${codex_exec_args[@]}")
    fi

    # command_timeout.py holds an epoch deadline on purpose, so a sleeping host
    # spends the budget without running the model. This laptop sleeps after one
    # minute on battery, which killed nine of twelve local-research cycles at
    # exit 124 with no compute done. Hold an idle-sleep assertion for the
    # model's lifetime; caffeinate execs the child, so the timeout still owns
    # the process group.
    local -a power_prefix=()
    if command -v caffeinate >/dev/null 2>&1; then
        power_prefix=(caffeinate -i -m -s)
    else
        echo "[$(date -Iseconds)] warning: caffeinate absent; a host sleep will consume the run budget" >&2
    fi

    python3 "$SCRIPTS_DIR/command_timeout.py" --seconds "$ROUTINE_TIMEOUT_SECONDS" -- \
        "${power_prefix[@]}" \
        "${codex_env[@]}" codex "${codex_global_args[@]}" --ask-for-approval never exec \
        "${codex_exec_args[@]}" \
        "$codex_prompt"
}

# Extract a bounded, credential-screened tail of the model transcript.
#
# `model-execution-failed` on its own only ever meant "codex exited non-zero";
# the reason went to the runner's stdout, and every routine plist points that
# at /tmp, which macOS purges. 95 of 279 claims are failed and the evidence for
# them was deleted the same week it was written. Keeping the tail on the claim
# is what makes a failure diagnosable after the fact.
#
# Screening fails closed: if the credential guard cannot run, nothing is kept.
# A claim lives under $OV and is private, but the transcript is model output
# over private prompts and deserves the same gate the prompts get.
# Fallback runtime. Same adapter prompt, same sanitized environment, same
# timeout and wake wrapping as run_codex; the fences are Claude Code's own:
#   --permission-mode dontAsk   anything outside the allow rules is denied
#                               silently and returned to the model as an error
#   Edit/Write rules on $OV     the vault is the only writable tree; the
#                               Atelier checkout is readable, never editable
#   --tools                     no Bash unless the profile enables shell
#                               network; WebSearch/WebFetch only for web=live
#   --setting-sources ""        no user or project settings, so no user hooks,
#                               no MCP servers, no permission allowlists leak
#                               in (the user_config=ignore equivalent)
#   --strict-mcp-config         belt and braces for the MCP half of the above
# The prompt goes in on stdin so no variadic flag can swallow it, and the JSON
# envelope comes out on stdout; routine_fallback.py extract lifts the
# schema-validated object into the same result file routine_result.py attests.
run_claude() {
    local envelope_file tools_csv claude_exit env_name
    local -a claude_env claude_args allowed_rules power_prefix=()

    if ! command -v claude >/dev/null 2>&1; then
        echo "ERROR: claude not found on PATH" >&2
        return 127
    fi
    if [ "$ATELIER_ACCESS_MODE" != "read" ]; then
        echo "ERROR: claude fallback supports only atelier_access=read profiles" >&2
        return 2
    fi
    resolve_routine_command || return $?
    render_adapter_prompt "natively" || return 2

    claude_env=(
        env -i
        "HOME=$HOME"
        "PATH=$PATH"
        "OV=$OV"
        "TMPDIR=${TMPDIR:-/tmp}"
        "LANG=${LANG:-en_US.UTF-8}"
        "ATELIER_ACTIVE_RUNTIME=claude"
        "ATELIER_PYTHON=$ATELIER_PYTHON"
        "ATELIER_ROUTINE_CYCLE=$CYCLE"
        "ATELIER_ROUTINE_PROFILE=$ROUTINE_PROFILE"
        "ATELIER_ROUTINE_PERMISSIONS=$PERMISSION_ALLOWLIST"
        "ATELIER_SKIP_LOCK_TOUCH=1"
    )
    for env_name in DRY_RUN SSL_CERT_FILE; do
        if [ -n "${!env_name:-}" ]; then
            claude_env+=("$env_name=${!env_name}")
        fi
    done

    tools_csv="Read,Glob,Grep,Edit,Write"
    # Absolute-path rules: `//` anchors at the filesystem root, so "/$OV"
    # (OV already starts with a slash) is the documented `//Users/...` form.
    allowed_rules=(
        "Read(/$OV/**)"
        "Read(/$ATELIER_DIR/**)"
        "Edit(/$OV/**)"
        "Write(/$OV/**)"
    )
    if [ "$WEB_SEARCH_MODE" = "live" ]; then
        tools_csv="$tools_csv,WebSearch,WebFetch"
        allowed_rules+=("WebSearch" "WebFetch")
    fi
    if [ "$SHELL_NETWORK_MODE" != "disabled" ]; then
        tools_csv="$tools_csv,Bash"
        allowed_rules+=("Bash")
    fi

    ROUTINE_CWD=$(mktemp -d "${TMPDIR:-/tmp}/atelier-routine-cwd.XXXXXX")
    ROUTINE_RESULT_FILE=$(mktemp "${TMPDIR:-/tmp}/atelier-routine-result.XXXXXX")
    envelope_file=$(mktemp "${TMPDIR:-/tmp}/atelier-routine-envelope.XXXXXX")

    claude_args=(
        -p
        --permission-mode dontAsk
        --setting-sources ""
        --settings '{"disableAllHooks":true}'
        --strict-mcp-config
        --no-session-persistence
        --add-dir "$OV" "$ATELIER_DIR"
        --allowedTools "${allowed_rules[@]}"
        --tools "$tools_csv"
        --output-format json
        # Claude's validator rejects the draft `$schema` URI the shared file
        # carries for Codex, so hand it the same schema without that key.
        --json-schema "$(python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); d.pop("$schema", None); print(json.dumps(d))' "$ATELIER_DIR/harness/routine_result.schema.json")"
    )
    if [ -n "${ATELIER_FALLBACK_CLAUDE_MODEL:-}" ]; then
        claude_args+=(--model "$ATELIER_FALLBACK_CLAUDE_MODEL")
    fi

    if command -v caffeinate >/dev/null 2>&1; then
        power_prefix=(caffeinate -i -m -s)
    fi

    claude_exit=0
    (
        cd "$ROUTINE_CWD" \
        && printf '%s' "$ADAPTER_PROMPT" \
        | python3 "$SCRIPTS_DIR/command_timeout.py" --seconds "$ROUTINE_TIMEOUT_SECONDS" -- \
            "${power_prefix[@]}" \
            "${claude_env[@]}" claude "${claude_args[@]}" > "$envelope_file"
    ) || claude_exit=$?

    # Keep the operator-facing summary in the transcript without the whole
    # envelope: turns, denials, and the model's own result text.
    python3 - "$envelope_file" <<'PY' || true
import json, sys
from pathlib import Path
try:
    env = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception as exc:
    print(f"claude envelope unreadable: {exc}")
    raise SystemExit(0)
denials = env.get("permission_denials") or []
print(
    f"claude envelope: subtype={env.get('subtype')} is_error={env.get('is_error')} "
    f"turns={env.get('num_turns')} denials={len(denials)}"
)
for item in denials[:10]:
    print(f"  denied: {json.dumps(item)[:300]}")
result = env.get("result")
if isinstance(result, str) and result:
    print(f"claude result: {result[:1000]}")
PY
    if [ "$claude_exit" -ne 0 ]; then
        rm -f "$envelope_file"
        return "$claude_exit"
    fi
    if ! python3 "$SCRIPTS_DIR/routine_fallback.py" extract \
        --envelope "$envelope_file" --out "$ROUTINE_RESULT_FILE"; then
        rm -f "$envelope_file"
        return 1
    fi
    rm -f "$envelope_file"
    return 0
}

model_failure_detail() {
    python3 - "$1" "$SCRIPTS_DIR" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path

log = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
try:
    from routine_prompt_guard import check as credential_lines
    flagged = set(credential_lines(log))
    text = log.read_text(encoding="utf-8", errors="replace")
except Exception:
    print("transcript withheld: credential screening unavailable")
    raise SystemExit(0)

kept = [
    line.strip()
    for number, line in enumerate(text.splitlines(), start=1)
    if line.strip() and number not in flagged
]
if not kept:
    print("transcript empty")
    raise SystemExit(0)
detail = " ".join(kept[-20:])
print(detail[-500:])
PY
}

# Keep the screened transcript of a failed run, so the next person to look has
# more than a 500-character tail. Only failures are kept, only the newest few
# per routine, and lines the credential guard flags are replaced rather than
# stored. This lives beside routine_runs/ and routine_failures/ instead of in
# ~/Library/Logs because claims show three different machines running these,
# and the transcript is worth as little as the claim if it is on the wrong one.
# $2 is an optional suffix so a fallback cycle keeps both transcripts:
# <cycle>.codex.log for the failed primary and <cycle>.log for the fallback.
preserve_model_log() {
    python3 - "$1" "$SCRIPTS_DIR" "$OV" "$ROUTINE" "$CYCLE" "${2:-}" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path

source, scripts_dir, ov, routine, cycle, suffix = sys.argv[1:7]
if suffix:
    cycle = f"{cycle}.{suffix}"
sys.path.insert(0, scripts_dir)
try:
    from routine_prompt_guard import check as credential_lines

    log = Path(source)
    flagged = set(credential_lines(log))
    text = log.read_text(encoding="utf-8", errors="replace")
except Exception:
    raise SystemExit(0)

screened = "\n".join(
    "[line withheld: credential screening]" if number in flagged else line
    for number, line in enumerate(text.splitlines(), start=1)
)
directory = Path(ov) / "_meta" / "routine_logs" / routine
directory.mkdir(parents=True, exist_ok=True)
(directory / f"{cycle}.log").write_text(screened + "\n", encoding="utf-8")

keep = sorted(directory.glob("*.log"))[-10:]
for path in directory.glob("*.log"):
    if path not in keep:
        path.unlink(missing_ok=True)
print(str(directory / f"{cycle}.log"))
PY
}

MODEL_LOG=$(mktemp "${TMPDIR:-/tmp}/atelier-routine-model.XXXXXX")

runner_event "starting: runtime=$RUNTIME command=$COMMAND"
STARTED_AT=$(date +%s)
export ATELIER_ACTIVE_RUNTIME="$RUNTIME"

cd "$ATELIER_DIR"
MODEL_EXIT_CODE=0
RUN_STATUS="failed"
RUN_OUTCOME="failed"
RUN_OUTPUT_FILE=""
RUN_ERROR="model-execution-failed"
RUN_ERROR_DETAIL=""
run_codex > "$MODEL_LOG" 2>&1 || MODEL_EXIT_CODE=$?
# Still goes to the launchd log, exactly as before; the file only exists so the
# tail survives long enough to reach the claim.
cat "$MODEL_LOG"

# --- runtime fallback -----------------------------------------------------
# A declared fallback runtime re-executes the cycle when Codex failed without
# delivering. routine_fallback.py decides eligibility (never on a timeout or
# a runner preflight error) and labels the reason for the claim.
if [ "$MODEL_EXIT_CODE" -ne 0 ] && [ "$FALLBACK_RUNTIME" != "none" ]; then
    FALLBACK_VERDICT=$(python3 "$SCRIPTS_DIR/routine_fallback.py" decide \
        --exit-code "$MODEL_EXIT_CODE" --log "$MODEL_LOG" \
        --fallback-runtime "$FALLBACK_RUNTIME" 2>/dev/null \
        || printf '{"fallback": false, "reason": "decide-failed", "runtime": null}')
    FALLBACK_OK=$(printf '%s' "$FALLBACK_VERDICT" | python3 -c 'import json, sys; print("1" if json.load(sys.stdin).get("fallback") else "0")' 2>/dev/null || echo 0)
    FALLBACK_REASON=$(printf '%s' "$FALLBACK_VERDICT" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("reason", "unknown"))' 2>/dev/null || echo unknown)
    if [ "$FALLBACK_OK" = "1" ]; then
        PRIMARY_EXIT_CODE=$MODEL_EXIT_CODE
        PRIMARY_DETAIL=$(model_failure_detail "$MODEL_LOG")
        PRIMARY_LOG=$(preserve_model_log "$MODEL_LOG" "$RUNTIME")
        runner_event "primary runtime failed (exit $PRIMARY_EXIT_CODE, $FALLBACK_REASON): $PRIMARY_DETAIL"
        if [ -n "$PRIMARY_LOG" ]; then
            runner_event "primary transcript kept: $PRIMARY_LOG"
        fi
        FALLBACK_FROM="$RUNTIME"
        RUNTIME="$FALLBACK_RUNTIME"
        export ATELIER_ACTIVE_RUNTIME="$RUNTIME"
        FALLBACK_REASON_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$FALLBACK_REASON")
        FALLBACK_FIELDS=$(printf 'fallback_from = "%s"\nfallback_reason = %s\nprimary_exit_code = %s' "$FALLBACK_FROM" "$FALLBACK_REASON_TOML" "$PRIMARY_EXIT_CODE")
        runner_event "starting: runtime=$RUNTIME command=$COMMAND (fallback from $FALLBACK_FROM)"
        MODEL_EXIT_CODE=0
        : > "$MODEL_LOG"
        run_claude > "$MODEL_LOG" 2>&1 || MODEL_EXIT_CODE=$?
        cat "$MODEL_LOG"
    else
        runner_event "no runtime fallback: $FALLBACK_REASON"
    fi
fi

if [ "$MODEL_EXIT_CODE" -eq 0 ]; then
    if RESULT_ATTESTATION=$(python3 "$SCRIPTS_DIR/routine_result.py" "$ROUTINE" \
        --cycle "$CYCLE" \
        --claimed-at "$CLAIMED_AT" \
        --result-file "$ROUTINE_RESULT_FILE" 2>&1); then
        RUN_STATUS="completed"
        RUN_OUTCOME=$(printf '%s' "$RESULT_ATTESTATION" | python3 -c 'import json, sys; print(json.load(sys.stdin)["outcome"])')
        RUN_OUTPUT_FILE=$(printf '%s' "$RESULT_ATTESTATION" | python3 -c 'import json, sys; print(json.load(sys.stdin)["output_file"])')
        RUN_ERROR=""
        runner_event "delivery validated: outcome=$RUN_OUTCOME output=$RUN_OUTPUT_FILE"
        # Keep the successful transcript too. A miss in a delivered report
        # (2026-09-01: two model releases absent from a "35/46 surfaces
        # checked" sweep) was unauditable because only failures were kept.
        KEPT_LOG=$(preserve_model_log "$MODEL_LOG")
        if [ -n "$KEPT_LOG" ]; then
            runner_event "transcript kept: $KEPT_LOG"
        fi
    else
        RUN_ERROR="delivery-attestation-failed"
        # The specific reason only ever reached a /tmp stderr file that macOS
        # purges, so every past attestation failure had to be re-derived from a
        # six-field claim record. Keep it on the claim itself.
        RUN_ERROR_DETAIL=$(printf '%s' "$RESULT_ATTESTATION" | tr '\n' ' ' | cut -c1-500)
        runner_event "delivery validation failed: $RUN_ERROR_DETAIL"
        echo "[$(date -Iseconds)] ERROR: delivery validation failed: $RESULT_ATTESTATION" >&2
    fi
else
    RUN_ERROR_DETAIL=$(model_failure_detail "$MODEL_LOG")
    PRESERVED_LOG=$(preserve_model_log "$MODEL_LOG")
    runner_event "model execution failed (exit $MODEL_EXIT_CODE): $RUN_ERROR_DETAIL"
    if [ -n "$PRESERVED_LOG" ]; then
        runner_event "transcript kept: $PRESERVED_LOG"
    fi
fi
rm -f "$MODEL_LOG"

ENDED_AT=$(date +%s)
DURATION=$(( ENDED_AT - STARTED_AT ))

runner_event "finished: status=$RUN_STATUS duration=${DURATION}s"

# --- release lock + update claim file ------------------------------------

COMPLETED_AT="$(date -Iseconds)"

if [ "$RUN_STATUS" = "completed" ]; then
    if release_lock; then
        runner_event "lock release: $RELEASE_RESULT"
    else
        RUN_STATUS="completion-uncertain"
        RUN_ERROR="lock-release-failed"
        echo "[$(date -Iseconds)] ERROR: model completed but lock release failed: $RELEASE_RESULT" >&2
    fi
fi

if [ "$RUN_STATUS" = "completed" ] || [ "$RUN_STATUS" = "completion-uncertain" ]; then
    RUN_OUTCOME_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$RUN_OUTCOME")
    RUN_OUTPUT_FILE_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$RUN_OUTPUT_FILE")
    FINAL_DETAILS=$(printf 'outcome = %s\noutput_file = %s' "$RUN_OUTCOME_TOML" "$RUN_OUTPUT_FILE_TOML")
else
    RUN_ERROR_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$RUN_ERROR")
    FINAL_DETAILS=$(printf 'error = %s\nmodel_exit_code = %s' "$RUN_ERROR_TOML" "$MODEL_EXIT_CODE")
    if [ -n "$RUN_ERROR_DETAIL" ]; then
        RUN_ERROR_DETAIL_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$RUN_ERROR_DETAIL")
        FINAL_DETAILS=$(printf '%s\nerror_detail = %s' "$FINAL_DETAILS" "$RUN_ERROR_DETAIL_TOML")
    fi
fi

CLAIM_STATUS="$RUN_STATUS"
VERIFICATION_FIELD=""
if [ "$ROUTINE" = "autoevo-nightly" ] && [ "$RUN_STATUS" = "completed" ]; then
    CLAIM_STATUS="completion-uncertain"
    VERIFICATION_FIELD='verification = "pending"'
fi

write_claim <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
contract_version = 2
profile = "$ROUTINE_PROFILE"
profile_fingerprint = "$PROFILE_FINGERPRINT"
runtime = "$RUNTIME"
atelier_access = "$ATELIER_ACCESS_MODE"
shell_network = "$SHELL_NETWORK_MODE"
owner_generation = $OWNER_GENERATION
retry_authorized = $LOCK_RETRY_AUTHORIZED
claimed_at = "$CLAIMED_AT"
status = "$CLAIM_STATUS"
completed_at = "$COMPLETED_AT"
duration_seconds = $DURATION
$FINAL_DETAILS
$CLAIM_EVENT_FIELD
$FALLBACK_FIELDS
$VERIFICATION_FIELD
EOF

if [ "$RUN_STATUS" = "completed" ]; then
    if [ "$ROUTINE" = "autoevo-nightly" ]; then
        if POST_VERIFY_JSON=$(python3 "$SCRIPTS_DIR/autoevo_verify.py" \
            --cycle "$CYCLE" \
            --wrapper-log "$AUTOEVO_EVENT_LOG" \
            --allow-pending-claim \
            --json); then
            VERIFIED_SWEEPS=$(printf '%s' "$POST_VERIFY_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin)["sweeps_completed"])')
            VERIFICATION_COMMIT=$(printf '%s' "$POST_VERIFY_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin)["audit_commit"])')
            VERIFIED_AT="$(date -Iseconds)"
            write_claim <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
contract_version = 2
profile = "$ROUTINE_PROFILE"
profile_fingerprint = "$PROFILE_FINGERPRINT"
runtime = "$RUNTIME"
atelier_access = "$ATELIER_ACCESS_MODE"
shell_network = "$SHELL_NETWORK_MODE"
owner_generation = $OWNER_GENERATION
retry_authorized = $LOCK_RETRY_AUTHORIZED
claimed_at = "$CLAIMED_AT"
status = "completed"
completed_at = "$COMPLETED_AT"
duration_seconds = $DURATION
$FINAL_DETAILS
$CLAIM_EVENT_FIELD
$FALLBACK_FIELDS
verification = "passed"
verified_at = "$VERIFIED_AT"
verified_sweeps = $VERIFIED_SWEEPS
verification_commit = "$VERIFICATION_COMMIT"
EOF
            runner_event "post-run verification passed: sweeps=$VERIFIED_SWEEPS commit=$VERIFICATION_COMMIT"
        else
            POST_VERIFY_ERROR=$(printf '%s' "$POST_VERIFY_JSON" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except json.JSONDecodeError:
    print("autoevo verifier returned invalid output")
else:
    print(value.get("error", "autoevo verifier failed without an error"))
')
            POST_VERIFY_ERROR_TOML=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$POST_VERIFY_ERROR")
            write_claim <<EOF
routine = "$ROUTINE"
cycle_id = "$CYCLE"
machine = "$HOSTNAME"
contract_version = 2
profile = "$ROUTINE_PROFILE"
profile_fingerprint = "$PROFILE_FINGERPRINT"
runtime = "$RUNTIME"
atelier_access = "$ATELIER_ACCESS_MODE"
shell_network = "$SHELL_NETWORK_MODE"
owner_generation = $OWNER_GENERATION
retry_authorized = $LOCK_RETRY_AUTHORIZED
claimed_at = "$CLAIMED_AT"
status = "completion-uncertain"
completed_at = "$COMPLETED_AT"
duration_seconds = $DURATION
$FINAL_DETAILS
$CLAIM_EVENT_FIELD
$FALLBACK_FIELDS
error = "post-run-verification-failed"
verification = "failed"
verification_detail = $POST_VERIFY_ERROR_TOML
EOF
            RUN_FINALIZED=1
            runner_event "ERROR: post-run verification failed: $POST_VERIFY_ERROR"
            exit 2
        fi
    fi
    RUN_FINALIZED=1
    runner_event "done: claim updated, lock released"
    exit 0
fi

RUN_FINALIZED=1
if [ "$RUN_STATUS" = "completion-uncertain" ]; then
    echo "[$(date -Iseconds)] done: claim records completion uncertainty; lock was not released" >&2
    exit 2
fi
echo "[$(date -Iseconds)] done: claim updated, lock retained after failure" >&2
exit 1
