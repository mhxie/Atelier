"""smoke_common.py: the shared runner, assertion, and paths for every smoke check.

Split out of harness_smoke.py; harness_smoke.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PYTHON = sys.executable

class SmokeFailure(Exception):
    pass

def run(
    args: list[str],
    *,
    input_text: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> str:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [PYTHON, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
    )
    if result.returncode != 0:
        raise SmokeFailure(
            f"`{PYTHON} {' '.join(args)}` failed with exit {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout

def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
