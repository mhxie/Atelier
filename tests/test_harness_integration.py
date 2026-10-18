"""Registry, Codex edge, and runtime-selector integration tests."""

from __future__ import annotations

import json
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.support import (  # noqa: E402
    PYTHON,
    expect,
    run,
)
import harness_smoke  # noqa: E402


def check_runtime_selector() -> None:
    status = json.loads(run(["scripts/atelier_runtime.py", "status", "--json"]))
    expect(
        status["committed_default"] == "codex", "shipped runtime default must be Codex"
    )
    expect(
        set(status["available"]) == {"claude", "codex"},
        "runtime registry must expose both CLIs",
    )

    codex = run(
        [
            "scripts/atelier_runtime.py",
            "run",
            "--runtime",
            "codex",
            "--dry-run",
            "hi",
            "smoke",
        ]
    ).strip()
    expect(
        "codex -C" in codex and "'$hi smoke'" in codex, "Codex selector command drift"
    )

    claude = run(
        [
            "scripts/atelier_runtime.py",
            "run",
            "--runtime",
            "claude",
            "--non-interactive",
            "--dry-run",
            "lint",
        ]
    ).strip()
    expect(claude == "claude -p /lint", "Claude selector command drift")

    overridden = json.loads(
        run(
            ["scripts/atelier_runtime.py", "status", "--json"],
            env_overrides={"ATELIER_RUNTIME": "claude"},
        )
    )
    expect(
        (overridden["runtime"], overridden["source"]) == ("claude", "environment"),
        "runtime env override drift",
    )

def check_runtime_cue_syntax() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-cue-runtime-") as temp_dir:
        (Path(temp_dir) / "reflections").mkdir()
        codex = json.loads(
            run(
                ["scripts/cues.py", "--only", "weekly", "--json", "--runtime", "codex"],
                env_overrides={"OV": temp_dir},
            )
        )
        claude = json.loads(
            run(
                [
                    "scripts/cues.py",
                    "--only",
                    "weekly",
                    "--json",
                    "--runtime",
                    "claude",
                ],
                env_overrides={"OV": temp_dir},
            )
        )
        expect(
            len(codex) == 1 and "`$weekly`" in codex[0]["message"],
            "Codex cue syntax drift",
        )
        expect(
            len(claude) == 1 and "`/weekly`" in claude[0]["message"],
            "Claude cue syntax drift",
        )


class HarnessIntegrationTest(unittest.TestCase):
    test_runtime_selector = staticmethod(check_runtime_selector)
    test_runtime_cue_syntax = staticmethod(check_runtime_cue_syntax)


class QualityGateContractTest(unittest.TestCase):
    def discovered(self, *modules: str, tests: int = 0, errors: tuple[str, ...] = ()):
        return mock.patch.object(harness_smoke, "_discover",
                                 return_value=(list(modules or ["tests.test_fixture"]), tests, list(errors)))

    def run_main(self, returncodes: list[int]) -> tuple[int, list[mock._Call], mock.Mock]:
        results = [
            subprocess.CompletedProcess(["fixture"], code, "", "broken\n" if code else "")
            for code in returncodes
        ]
        original_run = subprocess.run
        with self.discovered(), mock.patch.object(
            harness_smoke.subprocess, "run", side_effect=results
        ) as run_mock, mock.patch.object(
            harness_smoke.shutil, "which", return_value="/uvx"
        ) as which_mock, contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            result = harness_smoke.main()
        self.assertIs(harness_smoke.subprocess.run, original_run)
        return result, run_mock.call_args_list, which_mock

    def test_required_failures_propagate_and_short_circuit(self) -> None:
        result, calls, which = self.run_main([7])
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        which.assert_not_called()

        result, calls, which = self.run_main([0, 7])
        self.assertEqual(result, 1)
        self.assertEqual(calls[0].args[0], [PYTHON, "scripts/harness_lint.py"])
        self.assertEqual(
            calls[1].args[0],
            [PYTHON, "-m", "unittest", "tests.test_fixture"],
        )
        which.assert_not_called()

    def test_ruff_findings_propagate_after_required_checks(self) -> None:
        result, calls, which = self.run_main([0, 0, 1])
        self.assertEqual(result, 1)
        self.assertEqual(calls[2].args[0][:6], ["uvx", "--offline", "ruff", "check", "--no-fix", "--no-fix-only"])
        which.assert_called_once()

    def test_summary_names_skipped_coverage(self) -> None:
        results = [
            subprocess.CompletedProcess(["fixture"], 0, "", ""),
            subprocess.CompletedProcess(["fixture"], 0, "", "..s\nRan 3 tests in 0.1s\n\nOK (skipped=1)\n"),
            subprocess.CompletedProcess(["fixture"], 2, "", ""),
        ]
        stdout = io.StringIO()
        with self.discovered(tests=3), mock.patch.object(harness_smoke.subprocess, "run", side_effect=results), \
                mock.patch.object(harness_smoke.shutil, "which", return_value="/uvx"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(harness_smoke.main(), 0)
        self.assertEqual(stdout.getvalue().splitlines()[-1],
                         "harness_smoke: clean; skipped coverage: tests.test_fixture skipped=1, ruff")

    def test_every_failing_module_is_reported_and_fails_the_gate(self) -> None:
        def run(command, **_kwargs):
            failed = command[-1] in {"tests.test_a", "tests.test_c"}
            return subprocess.CompletedProcess(command, int(failed), "", f"{command[-1]} {'broken' if failed else 'OK'}\n")

        stderr = io.StringIO()
        with self.discovered("tests.test_a", "tests.test_b", "tests.test_c"), \
                mock.patch.object(harness_smoke.subprocess, "run", side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(harness_smoke.main(), 1)
        self.assertIn("tests.test_a broken", stderr.getvalue())
        self.assertIn("tests.test_c broken", stderr.getvalue())
        self.assertNotIn("tests.test_b OK", stderr.getvalue())

    def test_unloadable_modules_and_missing_tests_fail_instead_of_shrinking_the_gate(self) -> None:
        cases = (
            (self.discovered(tests=1, errors=("ImportError: tests.test_broken\n",)), "could not load every module"),
            (self.discovered(tests=5), "ran 3 of 5 discovered tests"),
        )
        for discovered, failure in cases:
            stderr = io.StringIO()
            ran = subprocess.CompletedProcess(["fixture"], 0, "", "Ran 3 tests in 0.1s\n\nOK\n")
            with self.subTest(failure=failure), discovered, \
                    mock.patch.object(harness_smoke.subprocess, "run", return_value=ran), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                self.assertEqual(harness_smoke.main(), 1)
                self.assertIn(failure, stderr.getvalue())

    def test_pre_push_lints_the_pushed_commit_not_the_working_tree(self) -> None:
        # Nothing linted at push time, so a commit over budget left the machine while a dirty tree passed.
        with tempfile.TemporaryDirectory(prefix="atelier-pre-push-") as tmp:
            repo, stubs = Path(tmp) / "repo", Path(tmp) / "bin"
            (repo / "scripts/hooks").mkdir(parents=True)
            stubs.mkdir()
            (repo / "scripts/hooks/pre-push").write_text((harness_smoke.ROOT / "scripts/hooks/pre-push").read_text())
            (repo / "scripts/harness_lint.py").write_text(
                "import pathlib, sys\nsys.exit(int((pathlib.Path(__file__).parents[1] / 'BROKEN').exists()))\n")
            (stubs / "uv").write_text("#!/bin/sh\nexit 0\n")  # the privacy gate has its own tests
            (stubs / "uv").chmod(0o755)

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                                       *args], check=True, capture_output=True, text=True).stdout.strip()

            def push(sha: str, base: str) -> int:
                return subprocess.run(["bash", "scripts/hooks/pre-push"], cwd=repo, capture_output=True, text=True,
                                      input=f"refs/heads/main {sha} refs/heads/main {base}\n", timeout=60,
                                      env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}).returncode

            git("init", "-q")
            git("add", ".")
            git("commit", "-qm", "clean")
            clean = git("rev-parse", "HEAD")
            (repo / "BROKEN").write_text("over budget\n")
            git("add", "BROKEN")
            git("commit", "-qm", "broken")
            broken = git("rev-parse", "HEAD")
            (repo / "BROKEN").unlink()  # the working tree now lints clean
            self.assertEqual(push(clean, clean), 0)
            self.assertEqual(push(broken, clean), 1)
            self.assertEqual(git("worktree", "list").count("\n"), 0)  # scratch worktrees are removed

    def test_success_and_optional_ruff_unavailability_return_zero(self) -> None:
        for ruff_returncode in (0, 2):
            with self.subTest(ruff_returncode=ruff_returncode):
                result, calls, which = self.run_main([0, 0, ruff_returncode])
                self.assertEqual(result, 0)
                self.assertEqual(len(calls), 3)
                which.assert_called_once()
