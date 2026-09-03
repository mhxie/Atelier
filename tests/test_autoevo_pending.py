"""Tests for scripts/autoevo_pending.py (queue append dedupe + auto-dismiss).

Glitch (2026-08-22): the nightly command hand-wrote TOML and re-proposed
clusters the user had already dismissed; nothing deduped by peers. This
helper owns the queue writes and is the only sanctioned writer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(vault: Path, queue: Path, *argv: str, stdin: str | None = None) -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), *argv],
        cwd=REPO_ROOT,
        env={**os.environ, "OV": str(vault)},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode not in (0, 1):
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


def _entry(eid: str, peers: list[str], proposed_at: str = "2099-01-01", status: str = "pending") -> dict:
    return {
        "id": eid,
        "category": "redundant",
        "proposed_action": 'merge "quoted" notes\nsecond line',
        "evidence_summary": "3 peers, mode=real",
        "peers": peers,
        "proposed_at": proposed_at,
        "last_surfaced": proposed_at,
        "surface_count": 0,
        "status": status,
    }


class PendingQueueTest(unittest.TestCase):
    def test_append_escapes_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            out = _run(vault, queue, "append", "--entries", "-", stdin=json.dumps([_entry("a", ["wip/x.md", "wip/y.md"])]))
            self.assertEqual(out["appended"], ["a"])
            data = tomllib.loads(queue.read_text(encoding="utf-8"))
            self.assertEqual(data["pending"][0]["proposed_action"], 'merge "quoted" notes\nsecond line')
            self.assertEqual(data["pending"][0]["peers"], ["wip/x.md", "wip/y.md"])

    def test_append_skips_dismissed_cluster_within_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            queue.parent.mkdir(parents=True)
            queue.write_text(
                'schema_version = 1\n\n[[pending]]\nid = "old"\ncategory = "redundant"\n'
                'proposed_action = "merge"\nevidence_summary = "e"\nproposed_at = "2099-01-01"\n'
                'last_surfaced = "2099-01-20"\nsurface_count = 0\nstatus = "dismissed"\n'
                'peers = ["wip/y.md", "wip/x.md"]\n',
                encoding="utf-8",
            )
            # Same peers in a different order, 30 days later: skipped.
            out = _run(
                vault, queue, "append", "--entries", "-", "--today", "2099-02-19",
                stdin=json.dumps([_entry("new", ["wip/x.md", "wip/y.md"], "2099-02-19"), _entry("fresh", ["wip/z.md", "wip/w.md"], "2099-02-19")]),
            )
            self.assertEqual(out["appended"], ["fresh"])
            self.assertEqual(out["skipped"][0]["id"], "new")
            self.assertIn("old", out["skipped"][0]["reason"])
            # Past the dedupe window the cluster may be proposed again.
            out2 = _run(
                vault, queue, "append", "--entries", "-", "--today", "2099-06-01",
                stdin=json.dumps([_entry("later", ["wip/x.md", "wip/y.md"], "2099-06-01")]),
            )
            self.assertEqual(out2["appended"], ["later"])

    def test_malformed_entries_json_returns_error_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            proc = subprocess.run(
                [sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), "append", "--entries", "-"],
                cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, input="{not json",
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 2)
            out = json.loads(proc.stdout)
            self.assertIn("error", out)
            self.assertEqual(out["appended"], [])
            self.assertFalse(queue.exists())
            self.assertEqual(proc.stderr, "")

    def test_corrupted_queue_refuses_with_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            queue.parent.mkdir(parents=True)
            queue.write_text("[[pending]\nbroken", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), "append", "--entries", "-"],
                cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)},
                input=json.dumps([_entry("a", ["wip/x.md"])]),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 2)
            out = json.loads(proc.stdout)
            self.assertIn("unreadable", out["error"])
            self.assertEqual(queue.read_text(encoding="utf-8"), "[[pending]\nbroken")
            sidecar = queue.parent / (queue.name + ".new")
            self.assertTrue(sidecar.is_file(), "proposed entries must be parked in a sidecar")
            self.assertIn('id = "a"', sidecar.read_text(encoding="utf-8"))
            # A retry against the still-corrupt queue must not clobber the first sidecar.
            proc2 = subprocess.run(
                [sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), "append", "--entries", "-"],
                cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)},
                input=json.dumps([_entry("b", ["wip/y.md"])]),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc2.returncode, 2)
            out2 = json.loads(proc2.stdout)
            self.assertTrue(out2["sidecar"].endswith(".new-1"), out2)
            self.assertIn('id = "a"', sidecar.read_text(encoding="utf-8"))
            self.assertIn('id = "b"', Path(out2["sidecar"]).read_text(encoding="utf-8"))

    def test_invalid_entry_is_reported_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            bad = _entry("bad", ["wip/x.md"])
            bad["category"] = "mystery"
            proc = subprocess.run(
                [sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), "append", "--entries", "-"],
                cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, input=json.dumps([bad]),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0)  # completed run; invalid is data, not an exit code
            out = json.loads(proc.stdout)
            self.assertEqual(out["appended"], [])
            self.assertEqual(out["invalid"][0]["id"], "bad")
            self.assertFalse(queue.exists())

    def test_non_dict_entry_reported_invalid_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            out = _run(vault, queue, "append", "--entries", "-",
                       stdin=json.dumps(["just a string", _entry("ok", ["wip/x.md"])]))
            self.assertEqual(out["appended"], ["ok"])
            self.assertEqual(len(out["invalid"]), 1)
            self.assertIn("not an object", out["invalid"][0]["problems"][0])

    def test_auto_dismiss_by_age_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            old = _entry("old", ["wip/a.md"], "2099-01-01")
            skipped3 = _entry("skipped", ["wip/b.md"], "2099-02-01")
            skipped3["surface_count"] = 3
            fresh = _entry("fresh", ["wip/c.md"], "2099-02-01")
            _run(vault, queue, "append", "--entries", "-", stdin=json.dumps([old, skipped3, fresh]))
            out = _run(vault, queue, "auto-dismiss", "--today", "2099-02-05")
            ids = sorted(d["id"] for d in out["auto_dismissed"])
            self.assertEqual(ids, ["old", "skipped"])
            data = tomllib.loads(queue.read_text(encoding="utf-8"))
            by_id = {e["id"]: e for e in data["pending"]}
            self.assertEqual(by_id["old"]["status"], "auto-dismissed")
            self.assertEqual(by_id["fresh"]["status"], "pending")
            self.assertIn("dismiss_reason", by_id["skipped"])


class ResolutionTest(unittest.TestCase):
    def test_resolve_anchors_dedupe_on_decision_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            _run(vault, queue, "append", "--entries", "-", stdin=json.dumps([_entry("a", ["wip/x.md", "wip/y.md"], "2099-01-01")]))
            out = _run(vault, queue, "resolve", "--id", "a", "--status", "dismissed", "--reason", "user skipped", "--today", "2099-03-01")
            self.assertEqual(out["status"], "dismissed")
            data = tomllib.loads(queue.read_text(encoding="utf-8"))
            self.assertEqual(data["pending"][0]["resolved_at"], "2099-03-01")
            self.assertEqual(data["pending"][0]["dismiss_reason"], "user skipped")
            # 80 days after proposal but only 20 after the decision: still protected.
            out2 = _run(vault, queue, "append", "--entries", "-", "--today", "2099-03-21",
                        stdin=json.dumps([_entry("again", ["wip/y.md", "wip/x.md"], "2099-03-21")]))
            self.assertEqual(out2["appended"], [])
            self.assertIn("a (dismissed)", out2["skipped"][0]["reason"])
            # Resolving twice is refused.
            out3 = _run(vault, queue, "resolve", "--id", "a", "--status", "applied", "--reason", "merge is right")
            self.assertIn("error", out3)

    def test_defer_increments_and_feeds_auto_dismiss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            _run(vault, queue, "append", "--entries", "-", stdin=json.dumps([_entry("a", ["wip/x.md"], "2099-02-01")]))
            for day in ("2099-02-02", "2099-02-03", "2099-02-04"):
                out = _run(vault, queue, "defer", "--id", "a", "--today", day)
            self.assertEqual(out["surface_count"], 3)
            out = _run(vault, queue, "auto-dismiss", "--today", "2099-02-05")
            self.assertEqual([d["id"] for d in out["auto_dismissed"]], ["a"])



class AutoDismissDryRunTest(unittest.TestCase):
    def test_dry_run_lists_candidates_without_writing(self) -> None:
        """/triage shows the housekeeping candidates before approving them."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            queue = vault / "_meta" / "autoevo_pending.toml"
            _run(vault, queue, "append", "--entries", "-", stdin=json.dumps([
                _entry("old", ["wip/a.md"], proposed_at="2099-01-01"),
                _entry("fresh", ["wip/b.md"], proposed_at="2099-02-04"),
            ]))
            before = queue.read_text(encoding="utf-8")
            out = _run(vault, queue, "auto-dismiss", "--today", "2099-02-05", "--dry-run")
            self.assertTrue(out["dry_run"])
            self.assertEqual([d["id"] for d in out["auto_dismissed"]], ["old"])
            self.assertEqual(queue.read_text(encoding="utf-8"), before)
            statuses = {e["id"]: e["status"] for e in tomllib.loads(before)["pending"]}
            self.assertEqual(statuses["old"], "pending")



class DefaultWithVetoTest(unittest.TestCase):
    """time-stale-A entries under wip/research get a 14-day default; nothing else does."""

    def _stale(self, eid: str, peers: list[str]) -> dict:
        entry = _entry(eid, peers)
        entry["category"] = "time-stale-A"
        entry["proposed_action"] = "close or redate the lapsed plan"
        return entry

    def test_append_stamps_default_only_for_eligible_peers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, queue = Path(tmp), Path(tmp) / "q.toml"
            entries = [
                self._stale("e-wip", ["wip/plan.md"]),
                self._stale("e-refl", ["reflections/2099-01-01-reflection.md"]),
                _entry("e-redundant", ["wip/a.md", "wip/b.md"]),
            ]
            payload = json.dumps(entries)
            out = _run(vault, queue, "append", "--entries", "-", "--today", "2099-01-01", "--rule-defaults", stdin=payload)
            self.assertEqual(sorted(out["appended"]), ["e-redundant", "e-refl", "e-wip"])
            rows = {e["id"]: e for e in tomllib.loads(queue.read_text())["pending"]}
            self.assertEqual(rows["e-wip"]["default_action"], "stale-banner")
            self.assertEqual(rows["e-wip"]["default_at"], "2099-01-15")
            self.assertNotIn("default_at", rows["e-refl"])
            self.assertNotIn("default_at", rows["e-redundant"])

            before = _run(vault, queue, "veto-expired", "--today", "2099-01-14")
            self.assertEqual(before["expired"], [])
            due = _run(vault, queue, "veto-expired", "--today", "2099-01-15")
            self.assertEqual([e["id"] for e in due["expired"]], ["e-wip"])
            self.assertEqual(due["expired"][0]["peers"], ["wip/plan.md"])

            deferred = _run(vault, queue, "defer", "--id", "e-wip", "--today", "2099-01-10")
            self.assertEqual(deferred["default_at"], "2099-01-24")
            self.assertEqual(_run(vault, queue, "veto-expired", "--today", "2099-01-15")["expired"], [])

            vetoed = _run(vault, queue, "resolve", "--id", "e-wip", "--status", "dismissed",
                          "--reason", "user skipped", "--today", "2099-01-25")
            self.assertEqual(vetoed["status"], "dismissed")
            self.assertEqual(_run(vault, queue, "veto-expired", "--today", "2099-02-01")["expired"], [])

    def test_stamp_defaults_migrates_backlog_with_fresh_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, queue = Path(tmp), Path(tmp) / "q.toml"
            _run(vault, queue, "append", "--entries", "-", "--today", "2099-01-01",
                 stdin=json.dumps([self._stale("e-wip", ["wip/plan.md"]), self._stale("e-refl", ["reflections/r.md"])]))
            dry = _run(vault, queue, "stamp-defaults", "--today", "2099-02-01", "--dry-run")
            self.assertEqual([r["id"] for r in dry["stamped"]], ["e-wip"])
            self.assertNotIn("default_at", {e["id"]: e for e in tomllib.loads(queue.read_text())["pending"]}["e-wip"])
            out = _run(vault, queue, "stamp-defaults", "--today", "2099-02-01")
            rows = {e["id"]: e for e in tomllib.loads(queue.read_text())["pending"]}
            self.assertEqual(out["default_at"], "2099-02-15")
            self.assertEqual(rows["e-wip"]["default_at"], "2099-02-15")
            self.assertNotIn("default_at", rows["e-refl"])
            self.assertEqual(_run(vault, queue, "stamp-defaults", "--today", "2099-02-02")["stamped"], [])

    def test_append_without_rule_flag_leaves_defaults_to_the_judge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, queue = Path(tmp), Path(tmp) / "q.toml"
            _run(vault, queue, "append", "--entries", "-", "--today", "2099-01-01",
                 stdin=json.dumps([self._stale("e-wip", ["wip/plan.md"])]))
            rows = tomllib.loads(queue.read_text())["pending"]
            self.assertNotIn("default_action", rows[0])
            ledger = Path(tmp) / "decisions.jsonl"
            out = _run(vault, queue, "--ledger", str(ledger), "set-default", "--id", "e-wip", "--action", "dismiss",
                       "--today", "2099-01-05", "--reason", "3 of 3 similar past entries were dismissed")
            self.assertEqual(out["default_at"], "2099-01-19")
            line = json.loads(ledger.read_text().splitlines()[-1])
            self.assertEqual((line["by"], line["verdict"], line["class"]), ("precedent", "dismiss", "autoevo/time-stale-A"))
            due = _run(vault, queue, "veto-expired", "--today", "2099-01-19", "--apply-dismissals")
            self.assertEqual(due["dismissed"], ["e-wip"])
            self.assertEqual(due["expired"], [])
            row = tomllib.loads(queue.read_text())["pending"][0]
            self.assertEqual((row["status"], row["dismiss_reason"]), ("dismissed", "default after veto window"))
            refused = _run(vault, queue, "set-default", "--id", "e-wip", "--action", "dismiss", "--today", "2099-01-20")
            self.assertIn("not pending", refused["error"])

    def test_resolve_requires_a_reason_and_writes_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, queue, ledger = Path(tmp), Path(tmp) / "q.toml", Path(tmp) / "decisions.jsonl"
            _run(vault, queue, "append", "--entries", "-", "--today", "2099-01-01",
                 stdin=json.dumps([self._stale("e-wip", ["wip/plan.md"])]))
            proc = subprocess.run(
                [sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), "resolve", "--id", "e-wip", "--status", "dismissed"],
                cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 2, "resolve without --reason must be an argparse error")
            out = _run(vault, queue, "--ledger", str(ledger), "resolve", "--id", "e-wip", "--status", "dismissed",
                       "--reason", "plan is still active this quarter", "--today", "2099-01-03")
            self.assertEqual(out["status"], "dismissed")
            line = json.loads(ledger.read_text().splitlines()[-1])
            self.assertEqual(line["verdict"], "dismiss")
            self.assertEqual(line["reason"], "plan is still active this quarter")
            self.assertEqual(line["features"]["tier"], "wip")
            self.assertEqual(line["by"], "human")


if __name__ == "__main__":
    unittest.main()
