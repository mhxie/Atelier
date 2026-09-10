"""Registry, Codex edge, and runtime-selector integration tests."""

from __future__ import annotations

import json
import contextlib
import io
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
            ["scripts/atelier_runtime.py", "resolve", "--json"],
            env_overrides={"ATELIER_RUNTIME": "claude"},
        )
    )
    expect(
        overridden == {"runtime": "claude", "source": "environment"},
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
    def run_main(self, returncodes: list[int]) -> tuple[int, list[mock._Call], mock.Mock]:
        results = [
            subprocess.CompletedProcess(["fixture"], code, "", "broken\n" if code else "")
            for code in returncodes
        ]
        original_run = subprocess.run
        with mock.patch.object(
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
            [PYTHON, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        )
        which.assert_not_called()

    def test_ruff_findings_propagate_after_required_checks(self) -> None:
        result, calls, which = self.run_main([0, 0, 1])
        self.assertEqual(result, 1)
        self.assertEqual(calls[2].args[0][:4], ["uvx", "--offline", "ruff", "check"])
        which.assert_called_once()

    def test_success_and_optional_ruff_unavailability_return_zero(self) -> None:
        for ruff_returncode in (0, 2):
            with self.subTest(ruff_returncode=ruff_returncode):
                result, calls, which = self.run_main([0, 0, ruff_returncode])
                self.assertEqual(result, 0)
                self.assertEqual(len(calls), 3)
                which.assert_called_once()
