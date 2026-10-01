#!/usr/bin/env python3
"""Read-only deterministic readiness checks for autoevo-nightly.

The scheduled runner decides how to record or defer a blocked result. This
helper never writes an audit or touches Git state; its only write is the
`--touch-lock` activity marker used by interactive hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from _paths import _resolve_segment, tier_segments, vault_root

ATELIER_ROOT = Path(__file__).resolve().parents[1]
# Interactive hooks touch the lock on every prompt, tool call, and turn end,
# so its age is idle time rather than time since a session opened.
SESSION_LOCK_TTL_SECONDS = 60 * 60
SESSION_LOCK_NAME = "atelier-session-lock"
GENERIC_RETRY_DELAY_SECONDS = 60 * 60
LEGACY_OWNED_AUDIT_STATE = "autoevo-preflight-owned-audit.json"


class PreflightError(RuntimeError):
    """The deterministic preflight could not produce a trustworthy result."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


PrivacyProbe = Callable[[], dict[str, object]]
SemanticProbe = Callable[[], dict[str, object]]


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: float = 30,
    env: dict[str, str] | None = None,
) -> CommandResult:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"cannot run {command[0]}: {exc}") from exc
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _inside_worktree(vault: Path) -> bool:
    result = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=vault)
    return result.returncode == 0 and result.stdout.strip() == "true"


def session_lock_path(vault: Path) -> Path:
    return _resolve_segment(tier_segments()["meta"], vault) / SESSION_LOCK_NAME


def _default_privacy_probe() -> dict[str, object]:
    result = _run(
        [sys.executable, str(ATELIER_ROOT / "scripts" / "privacy_check.py"), "--json"],
        cwd=ATELIER_ROOT,
        timeout=120,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightError(
            "privacy_check did not return valid JSON: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        ) from exc
    if result.returncode not in (0, 1):
        raise PreflightError(
            "privacy_check failed: "
            f"{result.stderr.strip() or result.stdout.strip() or result.returncode}"
        )
    if not isinstance(payload, dict):
        raise PreflightError("privacy_check returned a non-object JSON value")
    return payload


def _default_semantic_probe() -> dict[str, object]:
    started = time.time()
    try:
        result = _run(
            [
                "uv",
                "run",
                "--offline",
                "--quiet",
                str(ATELIER_ROOT / "scripts" / "semantic.py"),
                "status",
                "--format",
                "json",
            ],
            cwd=ATELIER_ROOT,
            timeout=180,
        )
    except PreflightError as exc:
        return {
            "ready": False,
            "detail": str(exc),
            "duration_seconds": round(time.time() - started, 3),
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    ready = (result.returncode == 0 and isinstance(payload, dict)
             and payload.get("backend") == "qmd" and payload.get("ready") is True)
    detail = ""
    if not ready:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"semantic probe exited {result.returncode}"
        )[:500]
    return {
        "ready": ready,
        "detail": detail,
        "mode": "qmd",
        "result_count": payload.get("totalDocuments") if isinstance(payload, dict) else None,
        "duration_seconds": round(time.time() - started, 3),
    }


def inspect_preflight(
    *,
    vault: Path | None = None,
    lock_path: Path | None = None,
    now: float | None = None,
    privacy_probe: PrivacyProbe | None = None,
    semantic_probe: SemanticProbe | None = None,
    publication_boundary: bool = False,
) -> dict[str, object]:
    """Check live write gates; expensive input probes run only before drafting."""
    vault = (vault or vault_root()).resolve()
    cache = _resolve_segment(tier_segments()["cache"], vault)
    lock_path = lock_path or session_lock_path(vault)
    now = time.time() if now is None else now
    blockers: list[dict[str, object]] = []
    health: dict[str, object] = {
        "vault": str(vault),
        "git_worktree": False,
        "session_lock_age_seconds": None,
        "privacy_hits": None,
        "semantic_ready": None,
        "semantic_mode": None,
        "semantic_probe_seconds": None,
    }

    legacy_state = cache / LEGACY_OWNED_AUDIT_STATE
    try:
        legacy_state.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PreflightError(f"cannot inspect legacy audit state: {exc}") from exc
    else:
        detail = (
            f"legacy owned-audit state remains at {legacy_state}; review and "
            "migrate it and its referenced audit before running autoevo; "
            "preflight will not read, delete, or commit either file"
        )
        return {
            "ready": False,
            "gate": "legacy_audit_review_required",
            "detail": detail,
            "blockers": [{"gate": "legacy_audit_review_required", "detail": detail}],
            "health": health,
            "retry_after_epoch": None,
        }

    # A lock that cannot be recorded or read is no evidence of an idle user.
    if not lock_path.parent.is_dir() or lock_path.is_symlink():
        blockers.append(
            {
                "gate": "session_lock_unsafe",
                "detail": f"{lock_path} has no parent directory or is a symlink",
            }
        )
    elif lock_path.exists():
        try:
            age = max(0, int(now - lock_path.stat().st_mtime))
        except OSError as exc:
            blockers.append(
                {
                    "gate": "session_lock_unreadable",
                    "detail": f"cannot read session lock metadata: {exc}",
                }
            )
        else:
            health["session_lock_age_seconds"] = age

    if not _inside_worktree(vault):
        blockers.append(
            {
                "gate": "git_not_worktree",
                "detail": "$OV is not a Git work tree, so eligibility and rollback are unavailable",
            }
        )
    else:
        health["git_worktree"] = True

    lock_age = health["session_lock_age_seconds"]
    if isinstance(lock_age, int) and lock_age < SESSION_LOCK_TTL_SECONDS:
        blockers.append(
            {
                "gate": "session_active",
                "detail": (
                    f"session-active lock is {lock_age}s old, below the "
                    f"{SESSION_LOCK_TTL_SECONDS}s safety window"
                ),
            }
        )

    if not blockers and not publication_boundary:
        probe = privacy_probe or _default_privacy_probe
        privacy = probe()
        hits = privacy.get("hit_count", 0)
        if not isinstance(hits, int):
            raise PreflightError("privacy_check hit_count is not an integer")
        health["privacy_hits"] = hits
        if hits > 0:
            blockers.append(
                {
                    "gate": "privacy_hits",
                    "detail": f"privacy_check found {hits} public-bound hits",
                }
            )

    if not blockers and not publication_boundary:
        probe = semantic_probe or _default_semantic_probe
        semantic = probe()
        ready = semantic.get("ready")
        if not isinstance(ready, bool):
            raise PreflightError("semantic probe omitted boolean ready")
        health["semantic_ready"] = ready
        health["semantic_mode"] = semantic.get("mode")
        health["semantic_probe_seconds"] = semantic.get("duration_seconds")
        if not ready:
            blockers.append(
                {
                    "gate": "semantic_unavailable",
                    "detail": str(
                        semantic.get("detail") or "semantic readiness probe failed"
                    ),
                }
            )

    primary = blockers[0] if blockers else None
    retry_after_epoch: int | None = None
    if primary is not None:
        retry_delay = GENERIC_RETRY_DELAY_SECONDS
        if primary["gate"] == "session_active" and isinstance(lock_age, int):
            retry_delay = max(1, SESSION_LOCK_TTL_SECONDS - lock_age + 1)
        retry_after_epoch = int(now) + retry_delay
    return {
        "ready": not blockers,
        "gate": primary["gate"] if primary else None,
        "detail": primary["detail"] if primary else "",
        "blockers": blockers,
        "health": health,
        "retry_after_epoch": retry_after_epoch,
    }


def environment_blocker(exc: BaseException, *, now: float | None = None) -> dict[str, object]:
    """Blocked result for a preflight that could not even inspect the vault.

    Timeouts and storage errors (git hanging on a cloud-synced tree, a file
    the sync client has not materialized) are transient. They must become a
    deferred domain result for the next scheduled flow, never an ambiguous
    model failure that waits for a human, because nothing a human does
    differently fixes them.
    """
    now = time.time() if now is None else now
    return {
        "ready": False,
        "gate": "environment_unavailable",
        "detail": f"preflight could not inspect the vault: {exc}"[:400],
        "blockers": [{"gate": "environment_unavailable", "detail": str(exc)[:400]}],
        "health": {"vault": str(vault_root()), "environment_error": str(exc)[:400]},
        "retry_after_epoch": int(now) + GENERIC_RETRY_DELAY_SECONDS,
    }


def touch_session_lock() -> bool:
    """Record interactive activity unless a scheduled runtime opted out.

    Never creates the parent or follows a symlinked lock; preflight refuses
    to run on either, so the failure surfaces there.
    """
    if os.environ.get("ATELIER_SKIP_LOCK_TOUCH"):
        return False
    lock = session_lock_path(vault_root().resolve())
    if lock.is_symlink():
        raise OSError(f"session lock is a symlink: {lock}")
    lock.touch()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--touch-lock", action="store_true")
    args = parser.parse_args(argv)
    if args.touch_lock:
        touch_session_lock()
        return 0
    try:
        try:
            result = inspect_preflight()
        except (OSError, PreflightError) as exc:
            result = environment_blocker(exc)
    except (OSError, PreflightError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, sort_keys=True))
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("ready" if result["ready"] else f"blocked: {result['gate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
