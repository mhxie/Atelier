#!/bin/bash
# Machine-wide mutex for local routine model runs.
#
# The readiness gate in routine_runner.sh covers a host that is not ready yet.
# It does not cover the other half of the same catch-up burst: launchd delivers
# every missed calendar event at once, so several routines start within the
# same second on one machine. Every exit-124 recorded on 2026-08-31 sat in an
# overlapping pair, and autoevo-nightly's event log shows its deterministic
# preflight taking 15m33s while another routine claimed one second
# later. The installed plists are staggered onto a ~3.5-minute grid, which
# separates the scheduled fires and nothing else; the runner's own stagger is
# hash(hostname) % 120, identical for every routine on the same host.
#
# Waiting is preferred over deferring. Since the profile budgets landed on
# 2026-07-26 the fleet's successful runs have a median of 151s and a maximum of
# 1779s, so a queued routine usually just runs a few minutes late. A defer is
# far more expensive than it looks: `schedule_decision` treats a failed claim
# as terminal, so a lost cycle is not retried until the next scheduled
# occurrence, which for a weekly routine is a week.
#
# The mutex is a symlink whose target carries "<routine> <pid>". symlink(2) is
# atomic and fails with EEXIST, which is what matters when the contending
# starts land in the same second, and the payload travels with the link, so
# there is never a moment where the lock exists but its holder is unreadable.
# (An earlier directory form wrote pid and routine after mkdir; a waiter that
# read the empty pid in that window reaped a live holder, and two model runs
# won the same burst.) It keeps the primitive off $OV: the vault is a
# cloud-provider mount whose fcntl locks returned EDEADLK through August.
#
# Reaping a dead holder is serialized through a second, short-lived mkdir
# lock, and the dead link is renamed away before it is deleted. Without the
# serialization two waiters could both judge the holder dead, one could reap
# and re-acquire, and the other would then delete the new holder's lock.
#
# Usage:
#   routine_run_mutex.sh acquire <routine> <owner-pid> <wait-seconds> [path]
#       exit 0  acquired; nothing on stdout
#       exit 1  still held after the wait; prints the holding routine
#       exit 2  usage or environment error
#   routine_run_mutex.sh release <owner-pid> [path]
#       Releases only a mutex this pid holds, so a late release cannot steal
#       the mutex out from under the routine that acquired it next.
#   routine_run_mutex.sh status [path]
#       Prints "<routine> <pid>" when held, nothing when free.

set -euo pipefail

DEFAULT_PATH="${ATELIER_RUN_MUTEX_PATH:-$HOME/Library/Caches/com.atelier/routine-run.lock}"
POLL_SECONDS="${ATELIER_RUN_MUTEX_POLL_SECONDS:-5}"
# A reaper section is a few syscalls; a reaper lock older than this belongs to
# a reaper that was itself killed.
REAPER_STALE_SECONDS=60

mtime_of() {
    stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

# Prints "<routine> <pid>" for the lock at $1, or nothing when unreadable.
# Reads the symlink form, and the directory form an older runner may still
# hold across an upgrade.
holder_info() {
    if [ -L "$1" ]; then
        readlink "$1" 2>/dev/null || true
    elif [ -d "$1" ]; then
        local routine pid
        routine=$(cat "$1/routine" 2>/dev/null || true)
        pid=$(cat "$1/pid" 2>/dev/null || true)
        if [ -n "$pid" ]; then
            printf '%s %s\n' "$routine" "$pid"
        fi
    fi
    return 0
}

holder_routine() { holder_info "$1" | awk '{print $1}'; }
holder_pid() { holder_info "$1" | awk '{print $2}'; }

# Removes the lock at $1 if its holder is dead. Returns 0 when the path was
# cleared, 1 when it was left alone (holder alive, or another reaper busy).
reap_if_dead() {
    local path="$1" reaper="$1.reaper" pid
    if ! mkdir "$reaper" 2>/dev/null; then
        if [ $(( $(date +%s) - $(mtime_of "$reaper") )) -gt "$REAPER_STALE_SECONDS" ]; then
            rmdir "$reaper" 2>/dev/null || true
        fi
        return 1
    fi
    # Re-read under the reaper lock: the holder we judged may already have
    # been reaped and replaced by a live one.
    pid=$(holder_pid "$path")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        rmdir "$reaper"
        return 1
    fi
    echo "reaping stale mutex holder: routine=$(holder_routine "$path") pid=${pid:-none}" >&2
    # Rename first so the path is never observed half-deleted.
    if mv "$path" "$path.stale.$$" 2>/dev/null; then
        rm -rf "$path.stale.$$"
    fi
    rmdir "$reaper"
    return 0
}

case "${1:-}" in
    acquire)
        ROUTINE="${2:?Usage: routine_run_mutex.sh acquire <routine> <owner-pid> <wait-seconds> [path]}"
        OWNER_PID="${3:?Usage: routine_run_mutex.sh acquire <routine> <owner-pid> <wait-seconds> [path]}"
        WAIT_SECONDS="${4:?Usage: routine_run_mutex.sh acquire <routine> <owner-pid> <wait-seconds> [path]}"
        MUTEX_PATH="${5:-$DEFAULT_PATH}"
        if [[ ! "$ROUTINE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
            echo "ERROR: invalid routine name: $ROUTINE" >&2
            exit 2
        fi
        if [[ ! "$OWNER_PID" =~ ^[0-9]+$ ]] || [[ ! "$WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
            echo "ERROR: owner pid and wait seconds must be integers" >&2
            exit 2
        fi
        if ! mkdir -p "$(dirname "$MUTEX_PATH")" 2>/dev/null; then
            echo "ERROR: cannot create $(dirname "$MUTEX_PATH")" >&2
            exit 2
        fi
        DEADLINE=$(( $(date +%s) + WAIT_SECONDS ))
        while :; do
            if ln -s "$ROUTINE $OWNER_PID" "$MUTEX_PATH" 2>/dev/null; then
                if [ -L "$MUTEX_PATH" ]; then
                    exit 0
                fi
                # The path was a directory (an older runner's lock form), so
                # ln placed the link inside it. Undo that and treat it as held.
                rm -f "$MUTEX_PATH/$ROUTINE $OWNER_PID"
            fi
            if [ ! -L "$MUTEX_PATH" ] && [ ! -e "$MUTEX_PATH" ]; then
                # Nothing holds the path and we still could not create it:
                # the parent is unwritable. Not contention; do not wait on it.
                echo "ERROR: cannot create $MUTEX_PATH" >&2
                exit 2
            fi
            if [ ! -L "$MUTEX_PATH" ] && [ ! -d "$MUTEX_PATH" ]; then
                echo "ERROR: $MUTEX_PATH exists and is not a routine mutex" >&2
                exit 2
            fi
            HOLDER_PID=$(holder_pid "$MUTEX_PATH")
            if [ -z "$HOLDER_PID" ] && [ -d "$MUTEX_PATH" ] \
                && [ $(( $(date +%s) - $(mtime_of "$MUTEX_PATH") )) -le "$REAPER_STALE_SECONDS" ]; then
                # Directory form still being written by an older runner.
                HOLDER_ALIVE=1
            elif [ -n "$HOLDER_PID" ] && kill -0 "$HOLDER_PID" 2>/dev/null; then
                HOLDER_ALIVE=1
            else
                HOLDER_ALIVE=0
            fi
            # A holder killed without running its release (SIGKILL, panic,
            # power loss) would otherwise wedge every routine on this machine
            # until a person noticed. Reap it, then race for the path again
            # immediately; every other branch sleeps before retrying.
            if [ "$HOLDER_ALIVE" -eq 0 ] && reap_if_dead "$MUTEX_PATH"; then
                continue
            fi
            [ "$(date +%s)" -ge "$DEADLINE" ] && break
            sleep "$POLL_SECONDS"
        done
        HELD_BY=$(holder_routine "$MUTEX_PATH")
        printf '%s\n' "${HELD_BY:-unknown}"
        exit 1
        ;;
    release)
        OWNER_PID="${2:?Usage: routine_run_mutex.sh release <owner-pid> [path]}"
        MUTEX_PATH="${3:-$DEFAULT_PATH}"
        if [ ! -L "$MUTEX_PATH" ] && [ ! -d "$MUTEX_PATH" ]; then
            exit 0
        fi
        if [ "$(holder_pid "$MUTEX_PATH")" != "$OWNER_PID" ]; then
            echo "refusing to release a mutex held by pid $(holder_pid "$MUTEX_PATH")" >&2
            exit 0
        fi
        if [ -L "$MUTEX_PATH" ]; then
            rm -f "$MUTEX_PATH"
        else
            rm -rf "$MUTEX_PATH"
        fi
        exit 0
        ;;
    status)
        MUTEX_PATH="${2:-$DEFAULT_PATH}"
        holder_info "$MUTEX_PATH"
        exit 0
        ;;
    *)
        echo "Usage: routine_run_mutex.sh {acquire|release|status} ..." >&2
        exit 2
        ;;
esac
