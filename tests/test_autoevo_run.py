"""autoevo_run owns the nightly mechanics the model used to re-execute inline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import _paths  # noqa: E402
import autoevo_run  # noqa: E402


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



class StaleBannerTest(unittest.TestCase):
    def test_banner_lands_after_frontmatter_and_h1_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            note = vault / "wip" / "plan.md"
            note.write_text("---\ntitle: Plan\n---\n\n# Plan\n\nShip by end of Q3 2025.\n", encoding="utf-8")
            first = _run(vault, "stale-banner", "--source", "wip/plan.md",
                         "--phrase", "by end of Q3 2025", "--entry-id", "20990101-time-stale-A-001",
                         "--run-date", "2099-01-15")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertTrue(json.loads(first.stdout)["changed"])
            lines = note.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[4], "# Plan")
            self.assertTrue(lines[6].startswith("> Stale since 2099-01-15 (autoevo 20990101-time-stale-A-001)"))
            self.assertIn("Ship by end of Q3 2025.", lines)
            again = _run(vault, "stale-banner", "--source", "wip/plan.md",
                         "--phrase", "by end of Q3 2025", "--entry-id", "20990101-time-stale-A-001",
                         "--run-date", "2099-01-16")
            self.assertFalse(json.loads(again.stdout)["changed"])
            self.assertEqual(note.read_text(encoding="utf-8").count("> Stale since"), 1)

    def test_banner_refused_outside_default_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            (vault / "reflections" / "r.md").write_text("# R\n", encoding="utf-8")
            proc = _run(vault, "stale-banner", "--source", "reflections/r.md",
                        "--phrase", "x", "--entry-id", "e", "--run-date", "2099-01-15")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("refused", json.loads(proc.stdout)["error"])
            self.assertEqual((vault / "reflections" / "r.md").read_text(encoding="utf-8"), "# R\n")



class InProgressOperationGateTest(unittest.TestCase):
    def test_plan_blocks_while_a_merge_is_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            git_dir = Path(_git(vault, "rev-parse", "--git-dir").stdout.strip())
            if not git_dir.is_absolute():
                git_dir = vault / git_dir
            (git_dir / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
            proc = _run(vault, "plan", "--run-ts", "20990101-050000", "--run-date", "2099-01-01")
            payload = json.loads(proc.stdout)
            gates = [b["gate"] for b in payload["gate"]["blockers"]]
            self.assertEqual(payload["gate"]["status"], "blocked", payload)
            self.assertIn("git_operation_in_progress", gates)
            (git_dir / "MERGE_HEAD").unlink()
            proc = _run(vault, "plan", "--run-ts", "20990101-050001", "--run-date", "2099-01-01")
            self.assertEqual(json.loads(proc.stdout)["gate"]["status"], "ready")



def _old(path: Path, days: int) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


class RouteBandsTest(unittest.TestCase):
    def test_rows_land_in_the_right_bucket_with_verified_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            for name in ("a", "b", "c", "d"):
                path = vault / "wip" / f"{name}.md"
                path.write_text(f"{name}\n", encoding="utf-8")
                _old(path, 60)
            fresh = vault / "wip" / "fresh.md"
            fresh.write_text("fresh\n", encoding="utf-8")
            outside = vault / "research" / "alpha" / "o.md"
            outside.write_text("o\n", encoding="utf-8")
            _old(outside, 60)
            low = vault / "wip" / "low.md"
            low.write_text("low\n", encoding="utf-8")
            _old(low, 400)
            mid = vault / "wip" / "mid.md"
            mid.write_text("mid\n", encoding="utf-8")
            _old(mid, 200)
            rows = [
                {"category": "redundant", "confidence": "high", "candidate": "wip/a.md", "peers": ["wip/b.md", "wip/c.md", "wip/d.md"], "scores": [0.9, 0.88, 0.86], "mode": "real"},
                {"category": "redundant", "confidence": "high", "candidate": "wip/a.md", "peers": ["wip/b.md", "wip/c.md", "wip/fresh.md"], "scores": [0.9, 0.88, 0.86], "mode": "real"},
                {"category": "redundant", "confidence": "high", "candidate": "wip/a.md", "peers": ["wip/b.md", "wip/c.md", "research/alpha/o.md"], "scores": [0.9, 0.88, 0.86], "mode": "real"},
                {"category": "redundant", "confidence": "high", "candidate": "wip/a.md", "peers": ["wip/b.md", "wip/c.md", "wip/d.md"], "scores": [0.9, 0.88, 0.86], "mode": "stub"},
                {"category": "redundant", "confidence": "medium", "candidate": "wip/a.md", "peers": ["wip/b.md", "wip/c.md"], "scores": [0.5, 0.4], "mode": "real"},
                {"category": "low-signal", "confidence": "high", "candidate": "wip/low.md", "conditions_met": 5},
                {"category": "low-signal", "confidence": "medium", "candidate": "wip/mid.md", "conditions_met": 5},
                {"category": "low-signal", "confidence": "high", "candidate": "wip/a.md", "conditions_met": 4},
                {"category": "time-stale-A", "confidence": "medium", "candidate": "wip/a.md", "peers": ["wip/a.md"]},
                {"category": "contradicted", "confidence": "low", "candidate": "wiki/x.md"},
            ]
            findings = vault / "findings.json"
            findings.write_text(json.dumps(rows), encoding="utf-8")
            from datetime import date as _date

            proc = _run(vault, "route-bands", "--findings", str(findings), "--today", _date.today().isoformat())
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out = json.loads(proc.stdout)
            auto = {r["index"]: r["band"] for r in out["auto_apply"]}
            pending = {r["index"]: r["route_reason"] for r in out["pending"]}
            invalid = {r["index"]: r["route_reason"] for r in out["invalid"]}
            probe = [r["index"] for r in out["probe"]]
            self.assertEqual(auto, {0: "redundant-high", 5: "low-signal-high"})
            self.assertIn("touched within 30d", pending[1])
            self.assertIn("outside wip", pending[2])
            self.assertIn("mode", pending[3])
            self.assertIn("below the pending floor", invalid[4])
            self.assertIn("cold", pending[6])
            self.assertIn("no flag", invalid[7])
            self.assertIn("intent-laden", pending[8])
            self.assertEqual(probe, [9])
            self.assertIn("0.85", out["auto_apply"][0]["band_label"])
            self.assertEqual(out["rules"]["redundant-high"]["cold_days"], 30)


class OpsTest(unittest.TestCase):
    def _snapshot(self, vault: Path, run_ts: str, *sources: str) -> dict:
        proc = _run(vault, "snapshot", "--run-ts", run_ts, *sum((["--source", s] for s in sources), []))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_identity_requires_a_cycle_when_unattended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            env = {**os.environ, "OV": str(vault), "ATELIER_ROUTINE_PROFILE": "local-maintenance"}
            env.pop("ATELIER_ROUTINE_CYCLE", None)
            proc = subprocess.run([sys.executable, "scripts/autoevo_run.py", "identity"], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("ATELIER_ROUTINE_CYCLE", json.loads(proc.stdout)["error"])
            env.pop("ATELIER_ROUTINE_PROFILE")
            proc = subprocess.run([sys.executable, "scripts/autoevo_run.py", "identity"], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
            out = json.loads(proc.stdout)
            self.assertRegex(out["run_ts"], r"^\d{8}-\d{6}$")
            self.assertFalse(out["unattended"])

    def test_verify_snapshot_refuses_a_changed_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            self._snapshot(vault, "20990101-050000", "wip/seed.md")
            ok = _run(vault, "verify-snapshot", "--run-ts", "20990101-050000", "--source", "wip/seed.md")
            self.assertEqual(ok.returncode, 0)
            (vault / "wip" / "seed.md").write_text("user edit mid-run\n", encoding="utf-8")
            changed = _run(vault, "verify-snapshot", "--run-ts", "20990101-050000", "--source", "wip/seed.md")
            self.assertEqual(changed.returncode, 2)
            self.assertIn("changed since its snapshot", json.loads(changed.stdout)["error"])

    def test_merge_op_commits_or_refuses_without_touching_a_changed_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            for name in ("a", "b", "c"):
                (vault / "wip" / f"{name}.md").write_text(f"note {name}\n", encoding="utf-8")
            _git(vault, "add", "-A")
            _git(vault, "commit", "-qm", "notes")
            run_ts = "20990101-050000"
            snap = self._snapshot(vault, run_ts, "wip/a.md", "wip/b.md", "wip/c.md")
            target = snap["target_rel"]
            body = vault / "cache" / "merged.md"
            body.write_text("# merged\n\nnote a + b + c\n", encoding="utf-8")
            # A user edit after the snapshot must stop the op before anything is written.
            (vault / "wip" / "b.md").write_text("note b (edited)\n", encoding="utf-8")
            refused = _run(vault, "merge-op", "--run-ts", run_ts, "--target", target, "--source", "wip/a.md", "--source", "wip/b.md", "--source", "wip/c.md",
                           "--body", str(body), "--scope", "wip", "--target-slug", "merged")
            self.assertEqual(refused.returncode, 2, refused.stdout)
            self.assertIn("changed since its snapshot", json.loads(refused.stdout)["error"])
            self.assertEqual((vault / "wip" / "b.md").read_text(encoding="utf-8"), "note b (edited)\n")
            self.assertEqual((vault / target).read_text(encoding="utf-8"), f"note {Path(target).stem}\n")
            # Restore and run for real.
            (vault / "wip" / "b.md").write_text("note b\n", encoding="utf-8")
            done = _run(vault, "merge-op", "--run-ts", run_ts, "--target", target, "--source", "wip/a.md", "--source", "wip/b.md", "--source", "wip/c.md",
                        "--body", str(body), "--scope", "wip", "--target-slug", "merged")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            out = json.loads(done.stdout)
            self.assertIn("sha", out)
            self.assertEqual((vault / target).read_text(encoding="utf-8"), "# merged\n\nnote a + b + c\n")
            survivors = {p.name for p in (vault / "wip").glob("*.md")}
            self.assertEqual(survivors, {"seed.md", Path(target).name})
            log = _git(vault, "log", "-1", "--format=%B").stdout
            self.assertIn(f"cluster_hash: {out['cluster_hash']}", log)
            self.assertIn("0.85", log)
            self.assertEqual(_git(vault, "status", "--porcelain").stdout.strip(), "")

    def test_archive_op_moves_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            run_ts = "20990101-050000"
            self._snapshot(vault, run_ts, "wip/seed.md")
            target = "archive/decayed/2099-01-01-wip-seed.md"
            done = _run(vault, "archive-op", "--run-ts", run_ts, "--source", "wip/seed.md", "--target", target,
                        "--slug", "seed", "--days-inactive", "400", "--evidence", "words: 1, links_in: 0, tags: 0, mtime: 2097-01-01")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertTrue((vault / target).is_file())
            self.assertFalse((vault / "wip" / "seed.md").exists())
            self.assertIn("[autoevo:low-signal] archive: seed", _git(vault, "log", "-1", "--format=%s").stdout)

    def test_stale_op_banners_commits_and_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            note = vault / "wip" / "plan.md"
            note.write_text("# Plan\n\nShip by end of Q3 2025.\n", encoding="utf-8")
            _git(vault, "add", "-A")
            _git(vault, "commit", "-qm", "plan")
            queue = vault / "_meta" / "autoevo_pending.toml"
            entry = {"id": "e1", "category": "time-stale-A", "proposed_action": "close it", "evidence_summary": "by end of Q3 2025",
                     "peers": ["wip/plan.md"], "proposed_at": "2099-01-01", "status": "pending"}
            subprocess.run([sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), "append", "--entries", "-", "--today", "2099-01-01"],
                           cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, input=json.dumps([entry]), capture_output=True, text=True, timeout=60, check=True)
            run_ts = "20990115-050000"
            self._snapshot(vault, run_ts, "wip/plan.md")
            ledger = vault / "_meta" / "decisions.jsonl"
            done = _run(vault, "stale-op", "--run-ts", run_ts, "--run-date", "2099-01-15", "--entry-id", "e1", "--source", "wip/plan.md",
                        "--phrase", "by end of Q3 2025", "--proposed-at", "2099-01-01", "--default-at", "2099-01-15", "--queue", str(queue), "--ledger", str(ledger))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            out = json.loads(done.stdout)
            self.assertTrue(out["changed"])
            self.assertEqual(out["resolved"]["status"], "applied")
            self.assertIn("> Stale since 2099-01-15", note.read_text(encoding="utf-8"))
            self.assertIn("[autoevo:time-stale-A] stale-banner: wip-plan", _git(vault, "log", "-1", "--format=%s").stdout)
            line = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual((line["by"], line["verdict"]), ("rule", "apply"))

    def test_low_signal_auto_apply_recomputes_conditions_instead_of_trusting_the_row(self) -> None:
        """`conditions_met` is the dispatched agent's own count, not evidence.

        A caller-supplied 5 used to be enough to reach an automatic archive with
        nothing checking the note's words, tags, inbound links, or tier.
        """
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)

            def band(candidate: str) -> tuple[str, str, str]:
                with mock.patch.dict(os.environ, {"OV": str(vault)}):
                    _paths.vault_root.cache_clear()
                    try:
                        return autoevo_run.route_row(
                            vault,
                            {"category": "low-signal", "confidence": "high",
                             "candidate": candidate, "conditions_met": 5},
                            date.today(),
                        )
                    finally:
                        _paths.vault_root.cache_clear()

            cases = {
                # old and cold, but 200 words: not low-signal, and the row cannot say otherwise
                "wip/wordy.md": (" ".join(["word"] * 200) + "\n", "200 words"),
                # short and cold, but deliberately tagged
                "wip/tagged.md": ("short note #keep\n", "#tags"),
            }
            for rel, (body, expected) in cases.items():
                note = vault / rel
                note.write_text(body, encoding="utf-8")
                _old(note, 400)
                with self.subTest(note=rel):
                    bucket, _, reason = band(rel)
                    self.assertEqual(bucket, "invalid")
                    self.assertIn(expected, reason)

            # all five hold: the automatic band is still reachable
            quiet = vault / "wip" / "quiet.md"
            quiet.write_text("short\n", encoding="utf-8")
            _old(quiet, 400)
            self.assertEqual(band("wip/quiet.md")[:2], ("auto_apply", "low-signal-high"))

    def test_stale_op_refuses_a_vetoed_entry_without_touching_the_note(self) -> None:
        """A human decision between `veto-expired` and this op wins.

        The queue state used to be checked only by `resolve_entry`, after the
        banner was written and committed: the op acted on a vetoed entry and
        still reported success with the refusal nested inside.
        """
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            note = vault / "wip" / "plan.md"
            original = "# Plan\n\nShip by end of Q3 2025.\n"
            note.write_text(original, encoding="utf-8")
            _git(vault, "add", "-A")
            _git(vault, "commit", "-qm", "plan")
            queue = vault / "_meta" / "autoevo_pending.toml"
            entry = {"id": "e1", "category": "time-stale-A", "proposed_action": "close it",
                     "evidence_summary": "by end of Q3 2025", "peers": ["wip/plan.md"],
                     "proposed_at": "2099-01-01", "status": "pending"}
            pend = [sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue)]
            env = {**os.environ, "OV": str(vault)}
            subprocess.run([*pend, "append", "--entries", "-", "--today", "2099-01-01"], cwd=REPO_ROOT,
                           env=env, input=json.dumps([entry]), capture_output=True, text=True, timeout=60, check=True)
            ledger = vault / "_meta" / "decisions.jsonl"
            # The user vetoes in /autoevo-review before the nightly reaches the op.
            subprocess.run([*pend, "--ledger", str(ledger), "resolve", "--id", "e1", "--status", "dismissed",
                            "--reason", "still live", "--today", "2099-01-14"], cwd=REPO_ROOT, env=env,
                           capture_output=True, text=True, timeout=60, check=True)

            run_ts = "20990115-050000"
            self._snapshot(vault, run_ts, "wip/plan.md")
            head_before = _git(vault, "rev-parse", "HEAD").stdout.strip()
            done = _run(vault, "stale-op", "--run-ts", run_ts, "--run-date", "2099-01-15", "--entry-id", "e1",
                        "--source", "wip/plan.md", "--phrase", "by end of Q3 2025", "--proposed-at", "2099-01-01",
                        "--default-at", "2099-01-15", "--queue", str(queue), "--ledger", str(ledger))
            self.assertNotEqual(done.returncode, 0, done.stdout)
            self.assertIn("not pending", json.loads(done.stdout)["error"])
            self.assertEqual(note.read_text(encoding="utf-8"), original)
            self.assertEqual(_git(vault, "rev-parse", "HEAD").stdout.strip(), head_before)

    def test_finalize_commits_audit_with_reports_and_quarantine_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            run_ts, run_date = "20990101-050000", "2099-01-01"
            audit_rel = f"agent-findings/autoevo-applied-{run_date}.md"
            (vault / audit_rel).write_text(
                f"## Autoevo Run: {run_date} 05:00\n\nRun ID: {run_ts}\n\n### Sweep coverage (0)\n\n### Sweep reports (1)\n- agent-findings/decay-{run_ts}-wip.md\n\n### Auto-applied (0)\n- (none)\n\n### Skipped (reason)\n- (none)\n\n### Errors\n- (none)\n",
                encoding="utf-8",
            )
            report_rel = f"agent-findings/decay-{run_ts}-wip.md"
            (vault / report_rel).write_text("# Decay Sweep\n", encoding="utf-8")
            outcomes = vault / "cache" / f"autoevo-{run_ts}-outcomes.json"
            outcomes.write_text("{}", encoding="utf-8")
            skipped = vault / "cache" / f"autoevo-{run_ts}-quarantine-skipped.txt"
            skipped.write_text("scope_quarantined: scope=/x (wip)\n", encoding="utf-8")
            reports = vault / "cache" / "reports.json"
            reports.write_text(json.dumps([report_rel]), encoding="utf-8")
            done = _run(vault, "finalize", "--run-ts", run_ts, "--run-date", run_date, "--audit-rel", audit_rel,
                        "--outcomes", str(outcomes), "--quarantine-skipped", str(skipped), "--reports", str(reports),
                        "--auto", "0", "--pending", "0", "--errors", "0")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            out = json.loads(done.stdout)
            self.assertIn("sha", out)
            self.assertIn("scope_quarantined: scope=/x (wip)", (vault / audit_rel).read_text(encoding="utf-8"))
            committed = _git(vault, "show", "--name-only", "--format=", "HEAD").stdout.split()
            self.assertIn(audit_rel, committed)
            self.assertIn(report_rel, committed)


if __name__ == "__main__":
    unittest.main()


class RecordUndosTest(unittest.TestCase):
    """A reverted autoevo op must reach the ledger as a human `undo`.

    Without this the judge's loudest failure signal is the one outcome
    `decisions.precedent_stats` never sees: the default fired and the user
    undid it.
    """

    def test_a_reverted_stale_banner_becomes_an_undo_line_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            ledger = vault / "_meta" / "decisions.jsonl"
            note = vault / "wip" / "plan.md"
            note.write_text("# Plan\n\n> Stale since 2099-01-01\n", encoding="utf-8")
            _git(vault, "add", "-A")
            _git(
                vault, "commit", "-q", "-m",
                "[autoevo:time-stale-A] stale-banner: wip-plan after 14d veto window\n\n"
                "Queue entry: e-42 (proposed 2099-01-01, default fired 2099-01-15)\n"
                "cluster_hash: deadbeef\n",
            )
            _git(vault, "revert", "--no-edit", "HEAD")

            out = _run(vault, "record-undos", "--today", "2099-02-01", "--ledger", str(ledger))
            self.assertEqual(json.loads(out.stdout)["recorded"], ["e-42"])
            line = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(
                (line["class"], line["subject"], line["verdict"], line["by"]),
                ("autoevo/time-stale-A", "e-42", "undo", "human"),
            )

            # Idempotent: the nightly runs this every cycle.
            again = json.loads(_run(vault, "record-undos", "--today", "2099-02-02", "--ledger", str(ledger)).stdout)
            self.assertEqual((again["recorded"], again["already_recorded"]), ([], ["e-42"]))
            self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 1)

    def test_a_reverted_non_autoevo_commit_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(tmp)
            ledger = vault / "_meta" / "decisions.jsonl"
            (vault / "wip" / "a.md").write_text("y\n", encoding="utf-8")
            _git(vault, "add", "-A")
            _git(vault, "commit", "-q", "-m", "ordinary edit by hand")
            _git(vault, "revert", "--no-edit", "HEAD")

            out = json.loads(_run(vault, "record-undos", "--today", "2099-02-01", "--ledger", str(ledger)).stdout)
            self.assertEqual(out["recorded"], [])
            self.assertFalse(ledger.exists())
