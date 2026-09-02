"""autoevo_run owns the nightly mechanics the model used to re-execute inline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(vault: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/autoevo_run.py", *argv],
        cwd=REPO_ROOT,
        env={**os.environ, "OV": str(vault)},
        capture_output=True,
        text=True,
        timeout=300,
    )


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def _make_vault(tmp: str) -> Path:
    # resolve() matches vault_root(); macOS tempdirs live behind /var -> /private/var.
    vault = Path(tmp).resolve() / "vault"
    for seg in (
        "wip",
        "research/alpha",
        "research/beta",
        "research/cache",
        "reflections",
        "cache",
        "archive",
        "agent-findings",
        "_meta",
    ):
        (vault / seg).mkdir(parents=True)
    (vault / "wip" / "seed.md").write_text("seed\n", encoding="utf-8")
    (vault / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(vault, "init", "-q")
    _git(vault, "config", "user.email", "test@example.com")
    _git(vault, "config", "user.name", "Test")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-qm", "seed")
    return vault


class PlanTest(unittest.TestCase):
    def test_fresh_session_lock_blocks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            (vault / "cache" / "atelier-session-lock").write_text("", encoding="utf-8")
            proc = _run(vault, "plan", "--run-ts", "t1", "--run-date", "2099-06-03")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["gate"]["status"], "blocked")
            self.assertEqual(out["gate"]["blockers"][0]["gate"], "session_lock_fresh")
            self.assertNotIn("dispatches", out)

    def test_ready_plan_rotates_and_filters_quarantine(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            # Quarantine research/alpha; day 3 over the 1 remaining live subdir
            # must pick beta, and the excluded `cache` subdir never appears.
            (vault / "_meta" / "autoevo_quarantine.toml").write_text(
                "[[quarantine]]\n"
                f'scope = "{vault / "research" / "alpha"}"\n'
                'first_failed = "2099-06-01"\n'
                "consecutive_failures = 3\n"
                'reason = "forgetter_no_envelope"\n'
                'expires_at = "2099-07-01"\n',
                encoding="utf-8",
            )
            proc = _run(vault, "plan", "--run-ts", "t2", "--run-date", "2099-06-03")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["gate"]["status"], "ready", out)
            scopes = [d["scope"] for d in out["dispatches"]]
            self.assertEqual(
                scopes,
                [
                    str(vault / "wip"),
                    str(vault / "research" / "beta"),
                    str(vault / "reflections"),
                ],
            )
            self.assertEqual(
                [d["max_candidates"] for d in out["dispatches"]], [12, 15, 12]
            )
            self.assertIn("research-tier rotation", out["quarantine_skipped"][0])
            outcomes = Path(out["outcomes_file"])
            self.assertEqual(outcomes.read_text(encoding="utf-8"), "{}")
            skipped_file = Path(out["quarantine_skipped_file"])
            self.assertIn("scope_quarantined:", skipped_file.read_text(encoding="utf-8"))

    def test_dirty_content_protects_and_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            (vault / "wip" / "dirty.md").write_text("x\n", encoding="utf-8")
            proc = _run(vault, "plan", "--run-ts", "t3", "--run-date", "2099-06-03")
            out = json.loads(proc.stdout)
            gates = [b["gate"] for b in out["gate"]["blockers"]]
            self.assertNotIn("dirty_autoevo_state", gates)
            self.assertIn("wip/dirty.md", out["protected_paths"])
            written = Path(out["protected_file"]).read_text(encoding="utf-8")
            self.assertIn("wip/dirty.md", written)

    def test_dirty_autoevo_state_still_blocks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            state = vault / "_meta" / "autoevo_pending.toml"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("# base\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", "--", "_meta/autoevo_pending.toml"],
                           cwd=vault, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "state"],
                           cwd=vault, check=True, capture_output=True)
            state.write_text("# dirty\n", encoding="utf-8")
            proc = _run(vault, "plan", "--run-ts", "t4", "--run-date", "2099-06-04")
            out = json.loads(proc.stdout)
            gates = [b["gate"] for b in out["gate"]["blockers"]]
            self.assertIn("dirty_autoevo_state", gates)


class OutcomeTest(unittest.TestCase):
    def test_records_and_rejects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            f = vault / "cache" / "autoevo-t-outcomes.json"
            f.write_text("{}", encoding="utf-8")
            proc = _run(
                vault, "outcome", "--file", str(f),
                "--scope", "/x/wip/", "--result", "envelope_returned",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                json.loads(f.read_text(encoding="utf-8")),
                {"/x/wip": "envelope_returned"},
            )
            bad = _run(
                vault, "outcome", "--file", str(f), "--scope", "/x", "--result", "nope"
            )
            self.assertEqual(bad.returncode, 2)


class TombstoneTest(unittest.TestCase):
    def test_layers_a_and_b(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            clean = _run(
                vault, "tombstone-check",
                "--source", "wip/a.md", "--source", "wip/b.md",
                "--today", "2099-06-03",
            )
            first = json.loads(clean.stdout)
            self.assertFalse(first["skip"])
            chash = first["cluster_hash"]

            # Layer B: explicit TOML tombstone for the same cluster.
            tomb = vault / "_meta" / "autoevo_tombstones.toml"
            tomb.write_text(
                f'[[tombstone]]\ncluster_hash = "{chash}"\nreason = "user said no"\n',
                encoding="utf-8",
            )
            hit = json.loads(
                _run(
                    vault, "tombstone-check",
                    "--source", "wip/a.md", "--source", "wip/b.md",
                    "--today", "2099-06-03",
                ).stdout
            )
            self.assertTrue(hit["skip"])
            self.assertIn("explicit tombstone", hit["reason"])
            tomb.unlink()

            # Layer A: reverted [autoevo:*] commit carrying the cluster_hash.
            (vault / "wip" / "merged.md").write_text("m\n", encoding="utf-8")
            _git(vault, "add", "wip/merged.md")
            _git(
                vault, "commit", "-qm",
                f"[autoevo:redundant] test merge\n\ncluster_hash: {chash}\n",
            )
            sha = _git(vault, "rev-parse", "HEAD").stdout.strip()
            _git(vault, "revert", "--no-edit", sha)
            hit = json.loads(
                _run(
                    vault, "tombstone-check",
                    "--source", "wip/a.md", "--source", "wip/b.md",
                    "--today", "2099-06-03",
                ).stdout
            )
            self.assertTrue(hit["skip"])
            self.assertIn("tombstoned cluster", hit["reason"])


class SnapshotStageRollbackTest(unittest.TestCase):
    def test_snapshot_picks_oldest_and_fails_atomically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            old, new = vault / "wip" / "old.md", vault / "wip" / "new.md"
            old.write_text("old\n", encoding="utf-8")
            new.write_text("new\n", encoding="utf-8")
            past = time.time() - 86400
            os.utime(old, (past, past))
            out = json.loads(
                _run(
                    vault, "snapshot", "--run-ts", "t4",
                    "--source", "wip/new.md", "--source", "wip/old.md",
                ).stdout
            )
            self.assertEqual(out["target_rel"], "wip/old.md")
            self.assertEqual(len(out["snapshots"]), 2)
            for snap in out["snapshots"]:
                self.assertTrue(Path(snap).is_file())
            missing = _run(
                vault, "snapshot", "--run-ts", "t4", "--source", "wip/absent.md"
            )
            self.assertEqual(missing.returncode, 2)
            self.assertIn("source missing", json.loads(missing.stdout)["error"])

    def test_stage_merge_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            a, b = vault / "wip" / "a.md", vault / "wip" / "b.md"
            a.write_text("a\n", encoding="utf-8")
            b.write_text("b\n", encoding="utf-8")
            _git(vault, "add", "-A")
            _git(vault, "commit", "-qm", "two notes")
            snap = json.loads(
                _run(
                    vault, "snapshot", "--run-ts", "t5",
                    "--source", "wip/a.md", "--source", "wip/b.md",
                ).stdout
            )
            target = snap["target_rel"]
            (vault / target).write_text("merged\n", encoding="utf-8")
            staged = json.loads(
                _run(
                    vault, "stage-merge", "--target", target,
                    "--source", "wip/a.md", "--source", "wip/b.md",
                ).stdout
            )
            self.assertEqual(sorted(staged["staged"]), ["wip/a.md", "wip/b.md"])
            other = "wip/b.md" if target == "wip/a.md" else "wip/a.md"
            self.assertFalse((vault / other).exists())

            # Simulated commit failure: rollback returns the tree to pre-op state.
            roll = json.loads(
                _run(
                    vault, "rollback", "--run-ts", "t5",
                    "--paths", target, other,
                    "--source", "wip/a.md", "--source", "wip/b.md",
                ).stdout
            )
            self.assertIn(target, roll["restored"])
            self.assertEqual((vault / "wip" / "a.md").read_text(encoding="utf-8"), "a\n")
            self.assertEqual((vault / "wip" / "b.md").read_text(encoding="utf-8"), "b\n")
            status = _git(vault, "status", "--porcelain").stdout.strip()
            self.assertEqual(status, "")

    def test_archive_target_collision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-run-") as tmp:
            vault = _make_vault(tmp)
            out = json.loads(
                _run(
                    vault, "archive-target",
                    "--source", "wip/stale.md", "--run-date", "2099-06-03",
                ).stdout
            )
            self.assertEqual(out["target_rel"], "archive/decayed/2099-06-03-wip-stale.md")
            (vault / out["target_rel"]).write_text("x\n", encoding="utf-8")
            dup = _run(
                vault, "archive-target",
                "--source", "wip/stale.md", "--run-date", "2099-06-03",
            )
            self.assertEqual(dup.returncode, 2)


if __name__ == "__main__":
    unittest.main()
