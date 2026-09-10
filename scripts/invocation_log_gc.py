#!/usr/bin/env python3
"""Age out legacy full-payload direct-API logs without creating new ones."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import time
from pathlib import Path


LOG_DIR = Path.home() / ".cache" / "atelier" / "llm_calls"
RETENTION_DAYS = 90


def rotate(log_dir: Path, retention_days: int, *, now: float | None = None) -> int:
    """Delete old regular ``*.jsonl`` children; return the removal count.

    The lifecycle caller always supplies the fixed legacy cache path. The path
    argument exists so tests can exercise disposable directories. Failures are
    best-effort, while non-positive retention and symlink directories fail safe.
    """
    if retention_days <= 0:
        sys.stderr.write(
            "invocation_log_gc: retention-days must be strictly positive; "
            "no files deleted\n"
        )
        return 0

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if log_dir.is_symlink():
            return 0
        directory_fd = os.open(log_dir, flags)
    except OSError:
        return 0

    cutoff = (time.time() if now is None else now) - retention_days * 86400
    removed = 0
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        os.unlink(entry.name, dir_fd=directory_fd)
                        removed += 1
                except OSError:
                    continue
    except OSError:
        pass
    finally:
        os.close(directory_fd)
    if removed:
        sys.stderr.write(f"invocation_log_gc: removed {removed} aged log file(s)\n")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    args = parser.parse_args(argv)
    rotate(LOG_DIR, args.retention_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
