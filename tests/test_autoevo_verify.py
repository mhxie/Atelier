"""The post-run verifier judges bot-owned dirt, not the user's.

Why this exists: `autoevo_verify._git_commit` failed any cycle whose vault
had a dirty path, while the preflight deliberately lets the sweep run on a
vault the user is editing (protected paths, out-of-scope paths). Measured on
2026-09-03 in the vault's claim files (`_meta/routine_runs/autoevo-nightly/`):
three of the four sweeps that ran in the prior ten days ended
`completion-uncertain` with "vault worktree is not clean after the cycle", and
the morning cue reported each as a missed run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import autoevo_verify  # noqa: E402

PREFIXES = ["wip", "research", "reflections", "agent-findings"]


class LeftoverPathsTest(unittest.TestCase):
    def test_user_dirt_is_not_a_leftover(self) -> None:
        entries = [
            ("??", "daily-notes/2099-01-03.md"),
            (" M", "wip/protected.md"),
            ("??", "personal/notes.md"),
        ]
        self.assertEqual(
            autoevo_verify.leftover_paths(entries, PREFIXES, {"wip/protected.md"}), []
        )

    def test_unprotected_in_scope_dirt_is_a_leftover(self) -> None:
        entries = [(" M", "wip/protected.md"), ("??", "wip/bot-left.md")]
        self.assertEqual(
            autoevo_verify.leftover_paths(entries, PREFIXES, {"wip/protected.md"}),
            ["wip/bot-left.md"],
        )

    def test_a_collapsed_untracked_directory_protects_its_files(self) -> None:
        # Plan time: `wip/newdir/` wholly untracked, one status entry. After
        # the bot commits one file inside it, git lists the sibling on its own.
        entries = [("??", "wip/newdir/user2.md"), ("??", "wip/other/left.md")]
        self.assertEqual(
            autoevo_verify.leftover_paths(entries, PREFIXES, {"wip/newdir/"}),
            ["wip/other/left.md"],
        )

    def test_autoevo_state_is_a_leftover_even_when_listed_protected(self) -> None:
        entries = [(" M", "_meta/autoevo_pending.toml")]
        self.assertEqual(
            autoevo_verify.leftover_paths(entries, PREFIXES, {"_meta/autoevo_pending.toml"}),
            ["_meta/autoevo_pending.toml"],
        )


def _git(vault: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(vault), *args], check=True, capture_output=True, text=True, timeout=60)


def _run_py(vault: Path, body: str) -> dict:
    code = (
        "import json, sys\nsys.path.insert(0, 'scripts')\nfrom pathlib import Path\n"
        "import autoevo_verify as v\n" + textwrap.dedent(body)
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={**os.environ, "OV": str(vault)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class GitCommitCheckTest(unittest.TestCase):
    def test_user_edits_survive_while_bot_leftovers_fail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            vault = Path(tmp).resolve() / "vault"
            for seg in ("wip", "research", "reflections", "agent-findings", "daily-notes", "cache", "_meta"):
                (vault / seg).mkdir(parents=True)
            (vault / ".gitignore").write_text("cache/\n", encoding="utf-8")
            (vault / "wip" / "seed.md").write_text("seed\n", encoding="utf-8")
            audit = vault / "agent-findings" / "autoevo-applied-2099-01-03.md"
            audit.write_text("## Autoevo Run: 2099-01-03 05:00\n", encoding="utf-8")
            _git(vault, "init", "-q")
            _git(vault, "config", "user.email", "test@example.com")
            _git(vault, "config", "user.name", "Test")
            _git(vault, "add", "-A")
            _git(vault, "commit", "-qm", "seed")
            # The user's night: an out-of-scope note plus an in-scope edit the
            # plan recorded as protected. Neither is the bot's to answer for.
            (vault / "daily-notes" / "2099-01-03.md").write_text("late note\n", encoding="utf-8")
            (vault / "wip" / "seed.md").write_text("seed edited\n", encoding="utf-8")
            (vault / "cache" / "autoevo-20990103-050000-protected.txt").write_text(
                "wip/seed.md\n", encoding="utf-8"
            )
            body = """
                audit = Path(sys.argv[1]) if len(sys.argv) > 1 else None
                vault = Path(%r)
                out = {}
                try:
                    out["commit"] = v._git_commit(vault, vault / "agent-findings" / "autoevo-applied-2099-01-03.md", "20990103-050000")
                except v.VerificationError as exc:
                    out["error"] = str(exc)
                print(json.dumps(out))
                """ % str(vault)
            out = _run_py(vault, body)
            self.assertIn("commit", out, out)
            # A file the bot left behind inside a sweep tier is a real leftover,
            # and the verifier must see it through the vault it was given even
            # when the ambient $OV names some other vault (`--vault` runs).
            (vault / "wip" / "bot-left.md").write_text("orphan\n", encoding="utf-8")
            for ov in (vault, Path(tmp) / "elsewhere"):
                out = _run_py(ov, body)
                self.assertIn("bot-owned paths are dirty", out.get("error", ""), (ov, out))
                self.assertIn("wip/bot-left.md", out["error"])
            # No protected list at all: the rule tightens and the error says so.
            (vault / "cache" / "autoevo-20990103-050000-protected.txt").unlink()
            out = _run_py(vault, body)
            self.assertIn("(no protected list for this run)", out.get("error", ""), out)


class ProtectedListReadTest(unittest.TestCase):
    """The protected list lives on the Drive-synced cache tier, which answers
    a read with EDEADLK while a file materializes (2026-08-27 and 08-28: the
    quarantine file failed the same way). One transient error must not turn
    the user's protected edits into bot leftovers."""

    def _read(self, vault: Path, run_id: str, raises: list[BaseException]) -> dict:
        from unittest import mock

        real = Path.read_text
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if raises:
                raise raises.pop(0)
            return real(self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", flaky), mock.patch("_paths.time.sleep"):
            protected, listed = autoevo_verify._protected_paths(vault, run_id)
        return {"protected": protected, "listed": listed, "reads": calls["n"]}

    def test_a_transient_mount_error_is_retried(self) -> None:
        import errno

        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            vault = Path(tmp).resolve()
            (vault / "cache").mkdir()
            (vault / "cache" / "autoevo-r1-protected.txt").write_text("wip/a.md\n", encoding="utf-8")
            out = self._read(vault, "r1", [OSError(errno.EDEADLK, "Resource deadlock avoided")])
            self.assertEqual(out["protected"], {"wip/a.md"}, out)
            self.assertTrue(out["listed"])
            self.assertEqual(out["reads"], 2)

    def test_a_missing_list_is_reported_not_retried(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            vault = Path(tmp).resolve()
            (vault / "cache").mkdir()
            out = self._read(vault, "r1", [])
            self.assertEqual((out["protected"], out["listed"]), (set(), False))
            self.assertEqual(out["reads"], 1)


class VerifierReadsTest(unittest.TestCase):
    """Every file the verifier inspects lives on the Drive-synced vault, so
    each read goes through the mount-aware retry, not only the protected list."""

    def _flaky_once(self):
        import errno
        from unittest import mock

        real = Path.read_text
        state = {"raised": False, "reads": 0}

        def flaky(self, *args, **kwargs):
            state["reads"] += 1
            if not state["raised"]:
                state["raised"] = True
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return real(self, *args, **kwargs)

        return mock.patch.object(Path, "read_text", flaky), mock.patch("_paths.time.sleep"), state

    def test_claim_read_survives_one_transient_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            claim = Path(tmp) / "2099-01-03.toml"
            claim.write_text('routine = "autoevo-nightly"\n', encoding="utf-8")
            patch_read, patch_sleep, state = self._flaky_once()
            with patch_read, patch_sleep:
                value = autoevo_verify._read_toml(claim)
            self.assertEqual(value, {"routine": "autoevo-nightly"})
            self.assertEqual(state["reads"], 2)

    def test_audit_read_survives_one_transient_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            audit = Path(tmp) / "autoevo-applied-2099-01-03.md"
            audit.write_text("## Autoevo Run: 2099-01-03 05:00\n\nRun ID: 20990103-050000\n", encoding="utf-8")
            patch_read, patch_sleep, state = self._flaky_once()
            with patch_read, patch_sleep:
                with self.assertRaises(autoevo_verify.VerificationError) as caught:
                    autoevo_verify._verify_audit(audit, minimum_sweeps=3)
            # The read succeeded on retry; what fails is the audit's content.
            self.assertNotIn("cannot read audit", str(caught.exception))
            self.assertEqual(state["reads"], 2)

    def test_sidecar_reads_survive_one_transient_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            vault = Path(tmp).resolve()
            (vault / "cache").mkdir()
            (vault / "cache" / "autoevo-r1-outcomes.json").write_text('{"wip": "swept"}', encoding="utf-8")
            (vault / "cache" / "autoevo-r1-lint.json").write_text('{"counts": {"error": 0}}', encoding="utf-8")
            patch_read, patch_sleep, state = self._flaky_once()
            with patch_read, patch_sleep:
                out = autoevo_verify._verify_sidecars(vault, "r1", {"wip": "swept"}, {"error": 0})
            self.assertTrue(out["outcomes"].endswith("autoevo-r1-outcomes.json"), out)
            self.assertEqual(state["reads"], 3)

    def test_wrapper_log_read_survives_one_transient_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            log = Path(tmp) / "wrapper.log"
            log.write_text(
                "[t0] claimed: /vault/_meta/routine_claims/2099-01-03.toml\n"
                "[t1] deterministic autoevo preflight passed\n"
                "[t2] starting: runtime=codex command=/autoevo-nightly\n"
                "[t3] delivery validated: outcome=delivered\n"
                "[t4] finished: status=completed\n"
                "[t5] lock release: ok\n",
                encoding="utf-8",
            )
            patch_read, patch_sleep, state = self._flaky_once()
            with patch_read, patch_sleep:
                out = autoevo_verify._verify_wrapper_log(log, "2099-01-03")
            self.assertEqual(len(out["markers_verified"]), 5)
            self.assertEqual(state["reads"], 2)


class SidecarNamingTest(unittest.TestCase):
    """One naming rule for every sidecar writer and reader; the nightly
    command's bash spells the lint sidecar by the same rule."""

    def test_writers_and_readers_share_the_rule(self) -> None:
        from autoevo_preflight import SIDECAR_SUFFIXES, autoevo_sidecar

        cache = Path("/cache")
        self.assertEqual(autoevo_sidecar(cache, "r1", "outcomes"), cache / "autoevo-r1-outcomes.json")
        self.assertEqual(autoevo_sidecar(cache, "r1", "protected").name, "autoevo-r1-protected.txt")
        with self.assertRaises(KeyError):
            autoevo_sidecar(cache, "r1", "snapshot")
        nightly = (REPO_ROOT / ".claude" / "commands" / "autoevo-nightly.md").read_text(encoding="utf-8")
        self.assertIn(autoevo_sidecar(cache, "${RUN_TS}", "lint").name, nightly)
        self.assertIn(autoevo_sidecar(cache, "${RUN_TS}", "reports").name, nightly)
        for rel in ("scripts/autoevo_run.py", "scripts/autoevo_verify.py"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for suffix in SIDECAR_SUFFIXES.values():
                self.assertNotIn(f"-{suffix}", source, (rel, suffix))


class GitReadRetryTest(unittest.TestCase):
    """The verifier's two git reads sit on the same mount as its file reads,
    so a transient failure of either is retried, not fatal."""

    def _repo(self, tmp: str) -> tuple[Path, Path, str]:
        vault = Path(tmp).resolve() / "vault"
        for seg in ("wip", "research", "reflections", "agent-findings", "cache", "_meta"):
            (vault / seg).mkdir(parents=True)
        (vault / ".gitignore").write_text("cache/\n", encoding="utf-8")
        audit = vault / "agent-findings" / "autoevo-applied-2099-01-03.md"
        audit.write_text("## Autoevo Run: 2099-01-03 05:00\n", encoding="utf-8")
        (vault / "agent-findings" / "sweep-2099-01-03.md").write_text("report\n", encoding="utf-8")
        (vault / "cache" / "autoevo-r1-protected.txt").write_text("", encoding="utf-8")
        _git(vault, "init", "-q")
        _git(vault, "config", "user.email", "test@example.com")
        _git(vault, "config", "user.name", "Test")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-qm", "seed")
        head = subprocess.run(
            ["git", "-C", str(vault), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()
        return vault, audit, head

    def _flaky_first(self, first):
        from unittest import mock

        real = subprocess.run
        state = {"calls": 0}

        def fake(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                if isinstance(first, BaseException):
                    raise first
                return first
            return real(*args, **kwargs)

        return mock.patch("_git.subprocess.run", fake), mock.patch("_paths.time.sleep"), state

    def test_audit_commit_read_survives_a_transient_spawn_error(self) -> None:
        import errno

        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            vault, audit, head = self._repo(tmp)
            patch_run, patch_sleep, state = self._flaky_first(OSError(errno.EDEADLK, "Resource deadlock avoided"))
            with patch_run, patch_sleep:
                commit = autoevo_verify._git_commit(vault, audit, "r1")
            self.assertEqual(commit, head)
            self.assertGreaterEqual(state["calls"], 2)

    def test_report_commit_read_survives_a_transient_exit(self) -> None:
        import errno
        import os as _os

        with tempfile.TemporaryDirectory(prefix="atelier-verify-") as tmp:
            vault, audit, head = self._repo(tmp)
            transient = subprocess.CompletedProcess(
                ["git"], 128, stdout="", stderr=f"fatal: unable to read: {_os.strerror(errno.EDEADLK)}\n"
            )
            patch_run, patch_sleep, state = self._flaky_first(transient)
            with patch_run, patch_sleep:
                verified = autoevo_verify._verify_reports(vault, audit, ["agent-findings/sweep-2099-01-03.md"], head)
            self.assertEqual(len(verified), 1)
            self.assertEqual(state["calls"], 2)


if __name__ == "__main__":
    unittest.main()
