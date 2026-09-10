"""command_timeout: epoch-based deadline, process-group stop, exit codes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
import command_timeout as ct  # noqa: E402


class CommandTimeoutTest(unittest.TestCase):
    def test_zero_grace_sends_only_sigkill(self) -> None:
        process = mock.Mock(pid=123, args=["fixture"])
        with mock.patch.object(ct.os, "killpg") as killpg, \
                mock.patch.object(ct, "wait_until_deadline") as wait:
            ct.stop_process_group(process, grace_seconds=0)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        wait.assert_not_called()
        process.wait.assert_called_once_with()

    def test_deadline_uses_the_injected_clock_not_elapsed_sleep(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            clock = iter([0.0, 100.0, 200.0])  # the machine "slept" 100s between polls
            with self.assertRaises(subprocess.TimeoutExpired):
                ct.wait_until_deadline(process, 10.0, now=lambda: next(clock), sleep=lambda _s: None)
        finally:
            ct.stop_process_group(process)
        self.assertIsNotNone(process.poll())

    def test_cli_kills_the_whole_group_and_returns_124(self) -> None:
        marker = Path(os.environ.get("TMPDIR", "/tmp")) / f"ct-child-{os.getpid()}.txt"
        marker.unlink(missing_ok=True)
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(30)\n"
        )
        started = time.time()
        proc = subprocess.run(
            [sys.executable, "scripts/command_timeout.py", "--seconds", "1", "--", sys.executable, "-c", script],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 124, proc.stderr)
        self.assertIn("timed out", proc.stderr)
        self.assertLess(time.time() - started, 15)
        child_pid = int(marker.read_text(encoding="utf-8"))
        marker.unlink(missing_ok=True)
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_cli_passes_through_exit_code_and_rejects_bad_args(self) -> None:
        ok = subprocess.run([sys.executable, "scripts/command_timeout.py", "--seconds", "5", "--", sys.executable, "-c", "raise SystemExit(3)"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(ok.returncode, 3)
        bad = subprocess.run([sys.executable, "scripts/command_timeout.py", "--seconds", "0", "--", "true"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 2)
        missing = subprocess.run([sys.executable, "scripts/command_timeout.py", "--seconds", "1", "--", "/nonexistent/binary"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(missing.returncode, 127)

    def test_cli_rejects_nonfinite_durations_before_spawning(self) -> None:
        for seconds in ("nan", "inf", "-inf", "1e999"):
            with self.subTest(seconds=seconds):
                result = subprocess.run(
                    [sys.executable, "scripts/command_timeout.py", f"--seconds={seconds}", "--",
                     sys.executable, "-c", "raise SystemExit(23)"],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("finite and positive", result.stderr)

    def test_cli_signals_stop_children_and_grandchildren(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), TemporaryDirectory() as temporary:
                marker = Path(temporary) / "ready.json"
                descendant = (
                    "import json, os, signal, time\nfrom pathlib import Path\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    f"Path({str(marker)!r}).write_text(json.dumps([os.getppid(), os.getpid()]))\n"
                    "time.sleep(30)\n"
                )
                command = (
                    "import subprocess, sys, time\n"
                    f"subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
                    "time.sleep(30)\n"
                )
                wrapper = subprocess.Popen(
                    [sys.executable, "scripts/command_timeout.py", "--seconds", "10", "--",
                     sys.executable, "-c", command],
                    cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True,
                )
                pids = []
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            pids = json.loads(marker.read_text())
                            break
                        except (FileNotFoundError, json.JSONDecodeError):
                            time.sleep(0.05)
                    self.assertEqual(len(pids), 2, "fixture children did not become ready")
                    wrapper.send_signal(signum)
                    _, stderr = wrapper.communicate(timeout=10)
                    self.assertEqual(wrapper.returncode, 128 + signum, stderr)
                    time.sleep(0.5)
                    for pid in pids:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                finally:
                    if pids:
                        try:
                            os.killpg(pids[0], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    wrapper.communicate(timeout=15)

    def test_cancellation_during_spawn_is_deferred_until_cleanup_is_possible(self) -> None:
        process = mock.Mock(args=["fixture"])
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        def spawn(*_args, **_kwargs):
            signal.raise_signal(signal.SIGTERM)
            return process

        for returncode in (None, 0, 3):
            with self.subTest(returncode=returncode), \
                    mock.patch.object(ct.subprocess, "Popen", side_effect=spawn), \
                    mock.patch.object(ct, "stop_process_group") as stop:
                process.poll.return_value = returncode
                with self.assertRaises(SystemExit) as raised:
                    ct.main(["--seconds", "1", "--", "fixture"])
                self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
                stop.assert_called_once_with(process)
                self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    def test_cli_normalizes_signal_terminated_child_status(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/command_timeout.py", "--seconds", "5", "--",
             sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)


if __name__ == "__main__":
    unittest.main()
