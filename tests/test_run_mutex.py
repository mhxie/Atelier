"""Tests for the machine concurrency gate in scripts/routine_run_mutex.sh.

Why this exists: nothing serialized routine model runs per machine. The
installed plists are staggered onto a ~3.5-minute grid, which separates the
scheduled fires and nothing else, and the runner's own stagger is
hash(hostname) % 120 -- identical for every routine on the same host. On a
launchd catch-up after the host slept, every missed event is delivered at once
and the grid provides no separation at all. All four exit-124 timeouts recorded
on 2026-08-31 sat in an overlapping pair.

These pin the properties the runner depends on: mutual exclusion under a
same-instant race, a wait that prefers queueing over losing a cycle, a reaper
so a killed holder cannot wedge the host, and a release that only the holder
can perform.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MUTEX = REPO_ROOT / "scripts" / "routine_run_mutex.sh"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged["ATELIER_RUN_MUTEX_POLL_SECONDS"] = "1"
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(MUTEX), *args],
        capture_output=True,
        text=True,
        env=merged,
    )


class RunMutexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "routine-run.lock")
        self.addCleanup(self._tmp.cleanup)

    def acquire(self, routine: str, pid: int, wait: int = 0):
        return run("acquire", routine, str(pid), str(wait), self.path)

    def test_a_free_mutex_is_acquired(self):
        result = self.acquire("routine-alpha", os.getpid())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            run("status", self.path).stdout.strip(),
            f"routine-alpha {os.getpid()}",
        )

    def test_a_second_routine_is_told_who_holds_it(self):
        """The caller needs the holder's name for the deferred claim."""
        self.assertEqual(self.acquire("autoevo-nightly", os.getpid()).returncode, 0)
        blocked = self.acquire("routine-bravo", os.getpid())
        self.assertEqual(blocked.returncode, 1)
        self.assertEqual(blocked.stdout.strip(), "autoevo-nightly")

    def test_release_frees_it_for_the_next_routine(self):
        self.assertEqual(self.acquire("routine-charlie", os.getpid()).returncode, 0)
        self.assertEqual(run("release", str(os.getpid()), self.path).returncode, 0)
        self.assertEqual(run("status", self.path).stdout.strip(), "")
        self.assertEqual(self.acquire("routine-delta", os.getpid()).returncode, 0)

    def test_only_the_holder_may_release(self):
        """A late release must not steal the mutex from whoever holds it now."""
        self.assertEqual(self.acquire("routine-echo", os.getpid()).returncode, 0)
        run("release", str(os.getpid() + 1), self.path)
        self.assertEqual(
            run("status", self.path).stdout.strip(),
            f"routine-echo {os.getpid()}",
        )

    def test_a_dead_holder_is_reaped_rather_than_wedging_the_host(self):
        """SIGKILL skips the runner's EXIT trap, so the mutex outlives it."""
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertEqual(self.acquire("routine-foxtrot", dead.pid).returncode, 0)
        result = self.acquire("routine-golf", os.getpid())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reaping stale mutex holder", result.stderr)
        self.assertEqual(
            run("status", self.path).stdout.strip(),
            f"routine-golf {os.getpid()}",
        )

    def test_waiting_beats_deferring_when_the_holder_finishes(self):
        """69-95s is the normal run, so a queued routine should just run."""
        self.assertEqual(self.acquire("routine-hotel", os.getpid()).returncode, 0)
        waiter = subprocess.Popen(
            ["bash", str(MUTEX), "acquire", "routine-india", str(os.getpid()), "30", self.path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "ATELIER_RUN_MUTEX_POLL_SECONDS": "1"},
        )
        try:
            self.assertIsNone(waiter.poll(), "waiter should block while the mutex is held")
            run("release", str(os.getpid()), self.path)
            self.assertEqual(waiter.wait(timeout=30), 0)
        finally:
            if waiter.poll() is None:
                waiter.kill()
            waiter.communicate()
        self.assertEqual(
            run("status", self.path).stdout.strip(),
            f"routine-india {os.getpid()}",
        )

    def test_only_one_of_a_same_instant_burst_wins(self):
        """The failure shape: launchd delivers every missed event at once."""
        starters = [
            subprocess.Popen(
                ["bash", str(MUTEX), "acquire", f"routine-{index}", str(os.getpid()), "0", self.path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "ATELIER_RUN_MUTEX_POLL_SECONDS": "1"},
            )
            for index in range(8)
        ]
        codes = []
        for process in starters:
            process.communicate(timeout=30)
            codes.append(process.returncode)
        self.assertEqual(codes.count(0), 1, f"exactly one acquire must win: {codes}")

    def test_a_bad_routine_name_is_refused(self):
        result = self.acquire("../escape", os.getpid())
        self.assertEqual(result.returncode, 2)

    def test_usage_errors_are_distinguishable_from_contention(self):
        """The runner treats exit 1 as contention and anything else as broken."""
        self.assertEqual(run("acquire", "x", "notapid", "0", self.path).returncode, 2)
        self.assertEqual(run("nonsense").returncode, 2)


if __name__ == "__main__":
    unittest.main()


class RunMutexRaceTests(unittest.TestCase):
    """Races the reviewer reproduced against the directory form of the mutex."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "routine-run.lock")
        self.addCleanup(self._tmp.cleanup)

    def test_concurrent_reapers_never_produce_two_holders(self):
        """A dead holder plus a same-instant burst: exactly one may win.

        The old form let a waiter reap the winner between its mkdir and its
        pid write, so two model runs started; it also let a second reaper
        delete the first reaper's freshly acquired lock.
        """
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        for _ in range(20):
            self.assertEqual(run("acquire", "seed", str(dead.pid), "0", self.path).returncode, 0)
            starters = [
                subprocess.Popen(
                    ["bash", str(MUTEX), "acquire", f"routine-{i}", str(os.getpid()), "0", self.path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={**os.environ, "ATELIER_RUN_MUTEX_POLL_SECONDS": "1"},
                )
                for i in range(4)
            ]
            results = [(p.wait(timeout=30), *p.communicate()) for p in starters]
            winners = [r for r in results if r[0] == 0]
            self.assertEqual(len(winners), 1, results)
            for code, out, _err in results:
                if code == 1:
                    self.assertNotEqual(out.strip(), "", "a loser must learn who holds the mutex")
                else:
                    self.assertEqual(code, 0, results)
            run("release", str(os.getpid()), self.path)

    def test_a_foreign_file_at_the_path_is_an_error_not_a_spin(self):
        """The old form looped without sleeping when mkdir failed for a
        reason other than contention, never reaching its deadline."""
        Path(self.path).write_text("not a lock\n", encoding="utf-8")
        result = run("acquire", "routine-x", str(os.getpid()), "5", self.path)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("not a routine mutex", result.stderr)

    def test_an_unwritable_parent_is_an_error_not_a_spin(self):
        parent = Path(self._tmp.name) / "ro"
        parent.mkdir()
        parent.chmod(0o500)
        self.addCleanup(parent.chmod, 0o700)
        result = run("acquire", "routine-x", str(os.getpid()), "5", str(parent / "lock"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("cannot create", result.stderr)

    def test_legacy_directory_lock_is_read_and_reaped(self):
        """An older runner may still hold the directory form across an upgrade."""
        Path(self.path).mkdir()
        (Path(self.path) / "pid").write_text(f"{os.getpid()}\n")
        (Path(self.path) / "routine").write_text("legacy\n")
        self.assertEqual(run("status", self.path).stdout.strip(), f"legacy {os.getpid()}")
        blocked = run("acquire", "routine-y", str(os.getpid()), "0", self.path)
        self.assertEqual(blocked.returncode, 1)
        self.assertEqual(blocked.stdout.strip(), "legacy")
        # A directory with no pid yet is a holder mid-write, not a stale lock.
        (Path(self.path) / "pid").unlink()
        self.assertEqual(run("acquire", "routine-y", str(os.getpid()), "0", self.path).returncode, 1)
        run("release", str(os.getpid()), self.path)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (Path(self.path) / "pid").write_text(f"{dead.pid}\n")
        result = run("acquire", "routine-y", str(os.getpid()), "0", self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(run("status", self.path).stdout.strip(), f"routine-y {os.getpid()}")
