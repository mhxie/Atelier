"""Resolve Node from system install locations, never a caller's search path."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import IO, Sequence


SYSTEM_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


class NodeError(RuntimeError):
    """Node was unreachable, or a bridge did not finish inside its timeout."""


def node_executable() -> Path:
    executable = shutil.which("node", path=SYSTEM_PATH)
    if executable is None:
        raise OSError("Node >=22 must be installed in a system or Homebrew prefix")
    return Path(executable).resolve(strict=True)


def system_env(*, passthrough: Sequence[str] = (), **extra: str) -> dict[str, str]:
    """Ambient-free child environment: our PATH, named passthrough keys, explicit extras."""
    environment = {"PATH": SYSTEM_PATH}
    environment.update({key: os.environ[key] for key in passthrough if key in os.environ})
    environment.update(extra)
    return environment


def run(
    argv: Sequence[object],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    input: str | None = None,
    stdout: IO[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Spawn a pinned Node script. A missing Node or a timeout raises NodeError."""
    streams = {"capture_output": True} if stdout is None else {"stdout": stdout}
    try:
        return subprocess.run(
            [str(node_executable()), *(str(part) for part in argv)],
            input=input,
            text=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=False,
            **streams,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NodeError(str(exc)) from exc
