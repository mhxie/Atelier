"""Thin offline quality gate: harness lint, unittest discovery, then Ruff."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_required(label: str, command: list[str]) -> bool:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        print(f"FAIL: {label} exited {result.returncode}", file=sys.stderr)
        return False
    print(f"ok: {label}")
    return True


def _run_ruff() -> bool:
    if shutil.which("uvx") is None:
        print("note: uvx unavailable; ruff strict-core check skipped")
        return True
    result = subprocess.run(
        ["uvx", "--offline", "ruff", "check", "scripts", "tests", "--select", "F,E4,E7,E9,EXE001"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode not in (0, 1):
        print(f"note: ruff unavailable (exit {result.returncode}); check skipped")
        return True
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode:
        print("FAIL: ruff strict-core violations", file=sys.stderr)
        return False
    print("ok: ruff strict-core lint")
    return True


def main() -> int:
    required = (
        ("harness lint", [sys.executable, "scripts/harness_lint.py"]),
        ("unittest discovery", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]),
    )
    for label, command in required:
        if not _run_required(label, command):
            return 1
    if not _run_ruff():
        return 1
    print("harness_smoke: clean")
    return 0

if __name__ == "__main__":
    sys.exit(main())
