#!/usr/bin/env python3
"""Run one command with a hard wall-clock timeout and signal its process group."""

from __future__ import annotations

import argparse
import atexit
from contextlib import suppress
import math
import os
import signal
import shutil
import subprocess
import sys
import time
from collections.abc import Callable

POLL_INTERVAL_SECONDS = 0.25


def wait_until_deadline(
    process: subprocess.Popen[bytes],
    deadline: float,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Wait using epoch time so a system sleep cannot pause the deadline."""
    while True:
        returncode = process.poll()
        if returncode is not None:
            return returncode
        remaining = deadline - now()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, 0)
        sleep(min(POLL_INTERVAL_SECONDS, remaining))


def stop_process_group(process: subprocess.Popen[bytes], *, grace_seconds: float = 5) -> None:
    """Terminate one group; callers with an exhausted deadline can skip grace."""
    if not math.isfinite(grace_seconds) or grace_seconds < 0:
        raise ValueError("grace_seconds must be finite and nonnegative")
    if grace_seconds:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return
        try:
            wait_until_deadline(process, time.time() + grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    # The leader may exit while a descendant ignores SIGTERM. Kill any
    # remaining group members before a caller releases its execution mutex.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _pipe_holders(process: subprocess.Popen) -> set[int]:
    """Find surviving macOS pipe writers after the leader exits."""
    descriptors = [str(stream.fileno()) for stream in (process.stdout, process.stderr)
                   if stream is not None and not stream.closed]
    if not descriptors:
        return set()

    def pipes(options):
        result = subprocess.run([shutil.which("lsof") or "/usr/sbin/lsof", "-nP", "-Fpdn", *options],
                                capture_output=True, text=True, timeout=2)
        if result.returncode not in (0, 1) or result.returncode and (result.stdout or result.stderr):
            result.check_returncode()
        pid, endpoint = None, None
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("f"):
                endpoint = None
            elif line.startswith("d"):
                endpoint = line[1:]
            elif line.startswith("n->") and endpoint is not None:
                yield pid, endpoint, line[3:]

    peers = {(peer, endpoint) for _pid, endpoint, peer in
             pipes(["-a", "-p", str(os.getpid()), "-d", ",".join(descriptors)])}
    if not peers:
        return set()
    candidates = {pid for pid, endpoint, peer in pipes(["-a", "-u", str(os.getuid())])
                  if pid != os.getpid() and (endpoint, peer) in peers}
    if not candidates:
        return set()
    return {pid for pid, endpoint, peer in pipes(["-a", "-p", ",".join(map(str, candidates))])
            if (endpoint, peer) in peers}


def stop_process_tree(process: subprocess.Popen) -> None:
    """Stop live descendants and orphaned holders of the captured output pipes."""
    process.poll()
    groups = {process.pid}
    holders = set()
    try:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGSTOP)
        table = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid="], text=True, timeout=2)
        parents = {int(pid): int(parent) for pid, parent in (line.split() for line in table.splitlines())}
        descendants = [process.pid]
        for parent in descendants:
            descendants.extend(pid for pid, ppid in parents.items() if ppid == parent and pid not in descendants)
        for pid in descendants:
            with suppress(ProcessLookupError):
                groups.add(os.getpgid(pid))
        holders = _pipe_holders(process)
    finally:
        try:
            for group in groups - {process.pid, os.getpgrp()}:
                with suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)
            stop_process_group(process, grace_seconds=0)
        finally:
            for pid in holders - {process.pid}:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


_LIVE_CHILDREN: dict[subprocess.Popen, float] = {}


def track_process(process: subprocess.Popen, *, grace_seconds: float = 5) -> None:
    _LIVE_CHILDREN[process] = grace_seconds


def release_process(process: subprocess.Popen) -> None:
    _LIVE_CHILDREN.pop(process, None)


def terminate_live_children() -> None:
    """Carry detached-child teardown through a host-owned sys.exit handler."""
    while _LIVE_CHILDREN:
        with suppress(KeyError, OSError, ValueError):
            process, grace = _LIVE_CHILDREN.popitem()
            stop_process_group(process, grace_seconds=grace)


atexit.register(terminate_live_children)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    cancelled = 0

    def cancel(signum, _frame):
        nonlocal cancelled
        cancelled = signum

    def clock():
        if cancelled:
            raise SystemExit(128 + cancelled)
        return time.time()

    handlers = {signum: signal.signal(signum, cancel) for signum in (signal.SIGINT, signal.SIGTERM)}
    process = None
    returncode = None
    try:
        try:
            process = subprocess.Popen(command, start_new_session=True)
        except OSError as exc:
            print(f"ERROR: cannot start {command[0]}: {exc}", file=sys.stderr)
            return 127
        try:
            returncode = wait_until_deadline(process, time.time() + args.seconds, now=clock)
            clock()
            return returncode if returncode >= 0 else 128 - returncode
        except subprocess.TimeoutExpired:
            print(
                f"ERROR: command timed out after {args.seconds:g}s: {command[0]}",
                file=sys.stderr,
            )
            return 124
    finally:
        try:
            if process is not None and (returncode is None or cancelled):
                stop_process_group(process)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
