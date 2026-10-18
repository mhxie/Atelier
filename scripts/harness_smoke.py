"""Thin offline quality gate: harness lint, unittest modules in parallel, then Ruff."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TIMEOUT = 1800


def _execute(command: list[str], deadline: float) -> subprocess.CompletedProcess[str] | None:
    # An unbounded wait would hang the gate on a test that blocks on a socket or stdin.
    try:
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                              timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return None


def _passed(label: str, result: subprocess.CompletedProcess[str] | None, skipped: list[str], *, echo: bool) -> bool:
    if result is None:
        print(f"FAIL: {label} exceeded {REQUIRED_TIMEOUT}s", file=sys.stderr)
        return False
    if echo or result.returncode:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        print(f"FAIL: {label} exited {result.returncode}", file=sys.stderr)
        return False
    if match := re.search(r"^OK \(.*\bskipped=(\d+)", result.stderr, re.M):
        skipped.append(f"{label} skipped={match[1]}")
    return True


def _discover() -> tuple[list[str], int, list[str]]:
    """Discovery's own modules and test count; a loader error fails the gate, never shrinks it."""
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))

    def modules(tests: unittest.TestSuite):
        for test in tests:
            yield from modules(test) if isinstance(test, unittest.TestSuite) else [type(test).__module__]

    return sorted(set(modules(suite))), suite.countTestCases(), list(loader.errors)


def _run_unittest(skipped: list[str]) -> bool:
    modules, expected, errors = _discover()
    if errors:
        print("".join(errors), end="", file=sys.stderr)
        print("FAIL: unittest discovery could not load every module", file=sys.stderr)
        return False
    deadline = time.monotonic() + REQUIRED_TIMEOUT
    # One process per module, a few at a time; eight starved a fixture's 30s codex timeout on a laptop.
    jobs = int(os.environ.get("ATELIER_SMOKE_JOBS") or min(4, os.cpu_count() or 1))
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        results = list(pool.map(lambda module: _execute([sys.executable, "-m", "unittest", module], deadline), modules))
    if not all([_passed(module, result, skipped, echo=False) for module, result in zip(modules, results)]):
        return False
    ran = sum(int(count) for result in results for count in re.findall(r"^Ran (\d+) tests?", result.stderr, re.M))
    if ran != expected:
        print(f"FAIL: unittest ran {ran} of {expected} discovered tests", file=sys.stderr)
        return False
    print(f"ok: unittest ({len(modules)} modules, {ran} tests)")
    return True


def _run_ruff(skipped: list[str]) -> bool:
    if shutil.which("uvx") is None:
        print("note: uvx unavailable; ruff strict-core check skipped")
        skipped.append("ruff")
        return True
    result = subprocess.run(
        ["uvx", "--offline", "ruff", "check", "--no-fix", "--no-fix-only",
         "scripts", "tests", "--select", "F,E4,E7,E9,EXE001"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode not in (0, 1):
        print(f"note: ruff unavailable (exit {result.returncode}); check skipped")
        skipped.append("ruff")
        return True
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode:
        print("FAIL: ruff strict-core violations", file=sys.stderr)
        return False
    print("ok: ruff strict-core lint")
    return True


def main() -> int:
    skipped: list[str] = []
    lint = _execute([sys.executable, "scripts/harness_lint.py"], time.monotonic() + REQUIRED_TIMEOUT)
    if not _passed("harness lint", lint, skipped, echo=True):
        return 1
    print("ok: harness lint")
    if not _run_unittest(skipped) or not _run_ruff(skipped):
        return 1
    # Skipped checks reduce coverage; the summary must not read as a full pass.
    print("harness_smoke: clean" + (f"; skipped coverage: {', '.join(skipped)}" if skipped else ""))
    return 0

if __name__ == "__main__":
    sys.exit(main())
