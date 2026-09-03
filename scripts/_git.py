#!/usr/bin/env python3
"""One git subprocess wrapper for every script that shells out to git.

Six scripts carried their own `subprocess.run(["git", ...])` with different
timeouts, error handling, and identity plumbing. This module owns:

  run_git(cwd, *args)        CompletedProcess, never raises on non-zero exit
  git_paths(cwd, *args)      NUL-separated path listing (ls-files and friends)
  merge_state(cwd)           names of in-progress git operations (merge,
                             rebase, cherry-pick, revert, bisect); the autoevo
                             gates refuse to commit while any is present
  BOT_NAME / BOT_EMAIL       the autoevo bot identity, applied with
                             bot_identity=True

Checkers (`harness_lint.py`, `privacy_check.py`) may use it: it wraps a
process call, not a registry parse, so it cannot mask a loader bug.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BOT_NAME = "Atelier Autoevo Bot"
BOT_EMAIL = "noreply@atelier.local"

# git-path names that exist only while an operation is in progress.
IN_PROGRESS_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
)


def bot_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": BOT_NAME,
        "GIT_AUTHOR_EMAIL": BOT_EMAIL,
        "GIT_COMMITTER_NAME": BOT_NAME,
        "GIT_COMMITTER_EMAIL": BOT_EMAIL,
    }


def run_git(
    cwd: Path,
    *args: str,
    timeout: float = 60,
    bot_identity: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run git in `cwd`. Non-zero exits are returned, not raised; OSError and
    TimeoutExpired propagate so callers decide whether that is fatal."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=text,
        timeout=timeout,
        check=False,
        env=bot_env() if bot_identity else None,
    )


def git_paths(cwd: Path, *args: str, timeout: float = 60) -> list[str]:
    """Sorted paths from a NUL-terminated git listing. Raises RuntimeError on
    a non-zero exit so a failed listing never reads as an empty repo."""
    result = run_git(cwd, *args, "-z", timeout=timeout, text=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return sorted(os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw)


def git_path(cwd: Path, name: str, timeout: float = 30) -> Path | None:
    """Resolve a git-dir entry (`index`, `MERGE_HEAD`, ...) to a filesystem path."""
    result = run_git(cwd, "rev-parse", "--git-path", name, timeout=timeout)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else cwd / path


def merge_state(cwd: Path, timeout: float = 30) -> list[str]:
    """Which in-progress operations (if any) the repository is in the middle of."""
    active: list[str] = []
    for marker in IN_PROGRESS_MARKERS:
        path = git_path(cwd, marker, timeout=timeout)
        if path is not None and path.exists():
            active.append(marker)
    return active
