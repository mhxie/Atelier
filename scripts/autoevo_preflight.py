#!/usr/bin/env python3
"""Read-only deterministic readiness checks for autoevo-nightly.

The scheduled runner decides how to record or defer a blocked result. This
helper never writes an audit, repairs Git state, commits, or pushes; its
only write is the `--touch-lock` activity marker used by interactive hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from _paths import _resolve_segment, tier_segments, vault_root

ATELIER_ROOT = Path(__file__).resolve().parents[1]
# Interactive hooks touch the lock on every prompt, tool call, and turn end,
# so its age is idle time rather than time since a session opened.
SESSION_LOCK_TTL_SECONDS = 60 * 60
GENERIC_RETRY_DELAY_SECONDS = 60 * 60
from _git import default_branch, merge_state  # noqa: E402

# Paths autoevo may touch: the three sweep scopes, the audit write target,
# and its queue files. The dirty-tree gate only looks here. The bot stages
# explicit paths and commits with `--only`, so user edits elsewhere in the
# vault cannot be swept into a bot commit; blocking on them only guaranteed
# the bot never ran on a vault that is dirty by design because it syncs
# through Drive.
AUTOEVO_SCOPE_TIERS = ("wip", "research", "reflections", "agent_findings")
# Any autoevo state file under _meta/ (pending queue, quarantine, tombstones,
# and future siblings) is gate input; match the documented `_meta/autoevo_*.toml`
# shape instead of enumerating names that can drift.
AUTOEVO_SCOPE_FILE_PREFIX = "_meta/autoevo_"
AUTOEVO_SCOPE_FILE_SUFFIX = ".toml"

def autoevo_scope_prefixes(vault: Path) -> list[str]:
    """Vault-relative prefixes the dirty gate inspects (posix, no trailing slash)."""
    prefixes: list[str] = []
    segments = tier_segments()
    for name in AUTOEVO_SCOPE_TIERS:
        segment = segments.get(name)
        if not segment:
            continue
        resolved = _resolve_segment(segment, vault)  # same resolver as tier(); never re-implement it
        try:
            rel = resolved.resolve().relative_to(vault.resolve()).as_posix()
        except ValueError:
            continue  # sandbox override outside the vault; not a git path here
        prefixes.append(rel.rstrip("/"))
    return prefixes


def _in_scope(path: str, prefixes: list[str]) -> bool:
    if _is_autoevo_state(path):
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _is_autoevo_state(path: str) -> bool:
    """True for autoevo's own queue and quarantine state under _meta/."""
    return path.startswith(AUTOEVO_SCOPE_FILE_PREFIX) and path.endswith(
        AUTOEVO_SCOPE_FILE_SUFFIX
    )


def partition_dirty_scope(status_paths: list[str], prefixes: list[str]) -> tuple[list[str], list[str]]:
    """Split dirty in-scope paths into blocking state and protected content.

    Dirty autoevo state means the queue is in an unknown condition, so the run
    cannot start. A dirty content file only means the user was editing it: the
    sweep runs and treats that file as untouchable. Blocking the whole sweep on
    it guaranteed the bot never ran on a vault the user actually works in.
    """
    blocking = sorted({p for p in status_paths if _is_autoevo_state(p)})
    protected = sorted(
        {p for p in status_paths if _in_scope(p, prefixes) and not _is_autoevo_state(p)}
    )
    return blocking, protected


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


def _git(vault: Path, *args: str, timeout: float = 30) -> CommandResult:
    return _run(["git", *args], cwd=vault, timeout=timeout)


def _git_path(vault: Path, name: str) -> Path:
    result = _git(vault, "rev-parse", "--git-path", name)
    if result.returncode != 0:
        raise PreflightError(
            f"cannot resolve Git path {name}: {result.stderr.strip() or 'unknown error'}"
        )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else (vault / path).resolve()


def _inside_worktree(vault: Path) -> bool:
    result = _git(vault, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def _status_entries(vault: Path) -> list[tuple[str, str]]:
    """Return (status code, vault-relative path) for every git status entry."""
    result = _git(vault, "--no-optional-locks", "status", "--porcelain=v1", "-z")
    if result.returncode != 0:
        raise PreflightError(
            f"git status failed: {result.stderr.strip() or 'unknown error'}"
        )
    prefix_result = _git(vault, "rev-parse", "--show-prefix")
    prefix = prefix_result.stdout.strip() if prefix_result.returncode == 0 else ""
    records = [raw for raw in result.stdout.split("\0") if raw]
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        code = record[:2] if len(record) >= 2 else "??"
        raw_paths = [record[3:] if len(record) > 3 else ""]
        if ("R" in code or "C" in code) and index < len(records):
            # Renames and copies emit the original path as the next record.
            # A `git mv wip/a.md personal/a.md` is in-scope dirt even though
            # its new path is not; count both ends.
            raw_paths.append(records[index])
            index += 1
        for raw_path in raw_paths:
            path = raw_path
            if prefix and path.startswith(prefix):
                path = path[len(prefix):]
            entries.append((code, path))
    return entries


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
    lock_path = lock_path or (cache / "atelier-session-lock")
    now = time.time() if now is None else now
    blockers: list[dict[str, object]] = []
    health: dict[str, object] = {
        "vault": str(vault),
        "git_worktree": False,
        "git_index": "unknown",
        "git_index_lock": "unknown",
        "worktree_entries": None,
        "worktree_entries_in_scope": None,
        "worktree_status_codes": {},
        "session_lock_age_seconds": None,
        "privacy_hits": None,
        "semantic_ready": None,
        "semantic_mode": None,
        "semantic_probe_seconds": None,
        "branch": "",
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

    if lock_path.exists():
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
                "detail": "$OV is not a Git work tree, so safe audit publication is unavailable",
            }
        )
    else:
        health["git_worktree"] = True
        index_path = _git_path(vault, "index")
        index_lock_path = _git_path(vault, "index.lock")
        index_exists = index_path.is_file()
        index_lock_exists = index_lock_path.exists()
        health["git_index"] = "present" if index_exists else "missing"
        health["git_index_lock"] = "present" if index_lock_exists else "absent"
        if not index_exists:
            blockers.append(
                {
                    "gate": "git_index_missing",
                    "detail": (
                        "Git index is missing; git status would misclassify tracked "
                        "files as mass deletions and untracked files"
                    ),
                }
            )
        if index_lock_exists:
            blockers.append(
                {
                    "gate": "git_index_lock_present",
                    "detail": (
                        "Git index.lock exists; autoevo will not delete or replace it"
                    ),
                }
            )
        try:
            in_progress = merge_state(vault)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PreflightError(f"cannot inspect git operation state: {exc}") from exc
        health["git_operation_in_progress"] = in_progress
        if in_progress:
            blockers.append(
                {
                    "gate": "git_operation_in_progress",
                    "detail": (
                        f"Git operation in progress ({', '.join(in_progress)}); a bot "
                        "commit would complete the user's merge, rebase, cherry-pick, or bisect"
                    ),
                }
            )

        if index_exists and not index_lock_exists:
            status_entries = _status_entries(vault)
            entries = len(status_entries)
            codes = dict(sorted(Counter(code for code, _ in status_entries).items()))
            health["worktree_entries"] = entries
            health["worktree_status_codes"] = codes
            prefixes = autoevo_scope_prefixes(vault)
            blocking, protected = partition_dirty_scope(
                [path for _, path in status_entries], prefixes
            )
            health["worktree_entries_in_scope"] = len(blocking) + len(protected)
            health["protected_paths"] = protected
            if blocking:
                sample = ", ".join(blocking[:3])
                blockers.append(
                    {
                        "gate": "dirty_autoevo_state",
                        "detail": (
                            f"$OV has {len(blocking)} changed autoevo state files "
                            f"(of {entries} total; a rename counts both ends), e.g. {sample}"
                        ),
                    }
                )
            health["branch"] = _git(vault, "branch", "--show-current").stdout.strip()
            health["default_branch"] = default_branch(vault)
            if not health["default_branch"] or health["branch"] != health["default_branch"]:
                blockers.append({
                    "gate": "git_not_default_branch",
                    "detail": "Autoevo requires a checked-out, identifiable default branch; it will not switch branches",
                })

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
    """Record interactive activity unless a scheduled runtime opted out."""
    if os.environ.get("ATELIER_SKIP_LOCK_TOUCH"):
        return False
    cache = _resolve_segment(tier_segments()["cache"], vault_root().resolve())
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "atelier-session-lock").touch()
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
