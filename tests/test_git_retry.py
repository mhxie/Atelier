"""run_git_retry survives the vault mount's transient errors, whether they
surface as a spawn OSError or as git's own non-zero exit, and retries nothing
else."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import _git  # noqa: E402

TRANSIENT_STDERR = f"fatal: unable to read .git/index: {os.strerror(errno.EDEADLK)}\n"


def _completed(returncode: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)


class RunGitRetryTest(unittest.TestCase):
    def _run(self, outcomes: list) -> tuple[subprocess.CompletedProcess, int]:
        calls = {"n": 0}

        def fake(*_args, **_kwargs):
            calls["n"] += 1
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(_git.subprocess, "run", fake), mock.patch("_paths.time.sleep"):
            result = _git.run_git_retry(ROOT, "log", "-1", what="test", attempts=3, delay=0)
        return result, calls["n"]

    def test_a_transient_exit_is_retried(self) -> None:
        result, calls = self._run([_completed(128, TRANSIENT_STDERR), _completed(0, stdout="abc\n")])
        self.assertEqual((result.returncode, result.stdout, calls), (0, "abc\n", 2))

    def test_a_transient_spawn_error_is_retried(self) -> None:
        result, calls = self._run([OSError(errno.EDEADLK, "Resource deadlock avoided"), _completed(0, stdout="abc\n")])
        self.assertEqual((result.returncode, calls), (0, 2))

    def test_an_ordinary_failure_is_returned_once(self) -> None:
        result, calls = self._run([_completed(128, "fatal: not a git repository\n")])
        self.assertEqual((result.returncode, calls), (128, 1))

    def test_the_last_transient_exit_is_returned_not_raised(self) -> None:
        result, calls = self._run([_completed(128, TRANSIENT_STDERR)] * 3)
        self.assertEqual((result.returncode, calls), (128, 3))
        self.assertIn(os.strerror(errno.EDEADLK), result.stderr)

    def test_the_last_transient_spawn_error_is_raised(self) -> None:
        with self.assertRaises(OSError) as caught:
            self._run([OSError(errno.EDEADLK, "Resource deadlock avoided")] * 3)
        self.assertEqual(caught.exception.errno, errno.EDEADLK)

    def test_a_non_transient_spawn_error_propagates(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self._run([FileNotFoundError(errno.ENOENT, "git")])

    def test_bytes_stderr_is_inspected_too(self) -> None:
        result, calls = self._run([_completed(128, TRANSIENT_STDERR.encode()), _completed(0)])
        self.assertEqual((result.returncode, calls), (0, 2))


if __name__ == "__main__":
    unittest.main()
