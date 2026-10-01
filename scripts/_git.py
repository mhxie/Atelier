#!/usr/bin/env python3
"""Shared git subprocess helpers.

Non-zero exits are returned for callers to interpret.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_git(
    cwd: Path,
    *args: str,
    timeout: float = 60,
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
    )


def git_paths(cwd: Path, *args: str, timeout: float = 60) -> list[str]:
    """Sorted paths from a NUL-terminated git listing. Raises RuntimeError on
    a non-zero exit so a failed listing never reads as an empty repo."""
    result = run_git(cwd, *args, "-z", timeout=timeout, text=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return sorted(os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw)


def tree_blobs(cwd: Path, *paths: str, timeout: float = 120) -> dict[str, str]:
    """Regular-file blob ids at HEAD under `paths`; empty for no paths or an unborn HEAD."""
    if not paths:
        return {}
    result = run_git(cwd, "--literal-pathspecs", "ls-tree", "-r", "-z", "HEAD", "--", *paths, timeout=timeout, text=False)
    blobs: dict[str, str] = {}
    for row in result.stdout.split(b"\0") if result.returncode == 0 else []:
        meta, _, name = row.partition(b"\t")
        fields = meta.split()
        if len(fields) == 3 and fields[0] in (b"100644", b"100755"):
            blobs[os.fsdecode(name)] = fields[2].decode("ascii")
    return blobs


def hash_objects(cwd: Path, paths: list[str], timeout: float = 120) -> list[str]:
    """Blob ids of the files' raw bytes (`hash-object --no-filters`, never `-w`)."""
    if not paths:
        return []
    result = run_git(cwd, "hash-object", "--no-filters", "--", *paths, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"git hash-object failed: {result.stderr.strip()[:200]}")
    return result.stdout.split()
