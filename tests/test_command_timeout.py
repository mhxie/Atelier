"""command_timeout: epoch-based deadline, process-group stop, exit codes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import command_timeout as ct  # noqa: E402


class CommandTimeoutTest(unittest.TestCase):
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
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
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


if __name__ == "__main__":
    unittest.main()
