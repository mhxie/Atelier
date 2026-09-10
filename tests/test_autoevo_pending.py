"""Tests for scripts/autoevo_pending.py (queue append dedupe + auto-dismiss).

Glitch (2026-08-22): the nightly command hand-wrote TOML and re-proposed
clusters the user had already dismissed; nothing deduped by peers. This
helper owns the queue writes and is the only sanctioned writer.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import autoevo_pending as pending  # noqa: E402


def _raw(vault: Path, queue: Path, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/autoevo_pending.py", "--queue", str(queue), *argv],
        cwd=REPO_ROOT,
        env={**os.environ, "OV": str(vault)},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run(
    vault: Path,
    queue: Path,
    *argv: str,
    stdin: str | None = None,
    expected_status: int = 0,
) -> dict:
    proc = _raw(vault, queue, *argv, stdin=stdin)
    if proc.returncode != expected_status:
        raise AssertionError(f"unexpected exit {proc.returncode}: {proc.stderr}")
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


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = Path(self.temporary.name)
        self.queue = self.vault / "_meta" / "autoevo_pending.toml"

    def command(self, *argv: str, stdin: str | None = None, expected_status: int = 0) -> dict:
        return _run(self.vault, self.queue, *argv, stdin=stdin, expected_status=expected_status)


class PendingQueueTest(QueueCase):
    def test_append_escapes_and_round_trips(self) -> None:
        self.queue = self.vault / "autoevo_pending.toml"
        self.queue.write_text(
            'schema_version = 1\nmarker = "keep"\n\n[metadata]\nowner = "legacy"\n\n'
            '[metadata.nested]\nenabled = true\n',
            encoding="utf-8",
        )
        entry = _entry("a", ["wip/x.md", "wip/y.md"])
        out = self.command("append", "--entries", "-", stdin=json.dumps([entry]))
        self.assertEqual(out["appended"], ["a"])
        data = tomllib.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(data["pending"][0]["proposed_action"], 'merge "quoted" notes\nsecond line')
        self.assertEqual(data["pending"][0]["peers"], ["wip/x.md", "wip/y.md"])
        self.assertEqual(data["marker"], "keep")
        self.assertEqual(data["metadata"], {"owner": "legacy", "nested": {"enabled": True}})
        self.assertEqual(
            list(data["pending"][0]),
            ["id", "category", "proposed_action", "evidence_summary", "proposed_at",
             "last_surfaced", "surface_count", "status", "peers"],
        )

    def test_append_skips_dismissed_cluster_within_window(self) -> None:
        self.queue.parent.mkdir(parents=True)
        self.queue.write_text(
            'schema_version = 1\n\n[[pending]]\nid = "old"\ncategory = "redundant"\n'
            'proposed_action = "merge"\nevidence_summary = "e"\nproposed_at = "2099-01-01"\n'
            'last_surfaced = "2099-01-20"\nsurface_count = 0\nstatus = "dismissed"\n'
            'peers = ["wip/y.md", "wip/x.md"]\n',
            encoding="utf-8",
        )
        out = self.command(
            "append", "--entries", "-", "--today", "2099-02-19",
            stdin=json.dumps([_entry("new", ["wip/x.md", "wip/y.md"], "2099-02-19"), _entry("fresh", ["wip/z.md", "wip/w.md"], "2099-02-19")]),
        )
        self.assertEqual(out["appended"], ["fresh"])
        self.assertEqual(out["skipped"][0]["id"], "new")
        self.assertIn("old", out["skipped"][0]["reason"])
        out = self.command(
            "append", "--entries", "-", "--today", "2099-06-01",
            stdin=json.dumps([_entry("later", ["wip/x.md", "wip/y.md"], "2099-06-01")]),
        )
        self.assertEqual(out["appended"], ["later"])

    def test_append_validation_cases(self) -> None:
        bad = _entry("bad", ["wip/x.md"])
        bad["category"] = "mystery"
        cases = (
            ("malformed JSON", "{not json", 2, None),
            ("invalid category", json.dumps([bad]), 0, "bad"),
        )
        for name, payload, status, invalid_id in cases:
            with self.subTest(case=name):
                proc = _raw(self.vault, self.queue, "append", "--entries", "-", stdin=payload)
                self.assertEqual(proc.returncode, status)
                out = json.loads(proc.stdout)
                self.assertEqual(out["appended"], [])
                self.assertFalse(self.queue.exists())
                self.assertEqual(proc.stderr, "")
                if invalid_id is None:
                    self.assertIn("error", out)
                else:
                    self.assertEqual(out["invalid"][0]["id"], invalid_id)

    def test_corrupted_queue_refuses_with_json_error(self) -> None:
        self.queue.parent.mkdir(parents=True)
        self.queue.write_text("[[pending]\nbroken", encoding="utf-8")
        sidecar = self.queue.parent / (self.queue.name + ".new")
        for eid, peer, expected_sidecar in (
            ("a", "wip/x.md", sidecar),
            ("b", "wip/y.md", self.queue.parent / (self.queue.name + ".new-1")),
        ):
            with self.subTest(attempt=eid):
                proc = _raw(
                    self.vault, self.queue, "append", "--entries", "-",
                    stdin=json.dumps([_entry(eid, [peer])]),
                )
                self.assertEqual(proc.returncode, 2)
                out = json.loads(proc.stdout)
                self.assertIn("unreadable", out["error"])
                self.assertEqual(self.queue.read_text(encoding="utf-8"), "[[pending]\nbroken")
                self.assertEqual(Path(out["sidecar"]), expected_sidecar)
                self.assertIn(f'id = "{eid}"', expected_sidecar.read_text(encoding="utf-8"))
        self.assertIn('id = "a"', sidecar.read_text(encoding="utf-8"))

    def test_non_dict_entry_reported_invalid_not_crash(self) -> None:
        out = self.command("append", "--entries", "-",
                       stdin=json.dumps(["just a string", _entry("ok", ["wip/x.md"])]))
        self.assertEqual(out["appended"], ["ok"])
        self.assertEqual(len(out["invalid"]), 1)
        self.assertIn("not an object", out["invalid"][0]["problems"][0])

    def test_auto_dismiss_by_age_and_count(self) -> None:
        old = _entry("old", ["wip/a.md"], "2099-01-01")
        skipped3 = _entry("skipped", ["wip/b.md"], "2099-02-01")
        skipped3["surface_count"] = 3
        fresh = _entry("fresh", ["wip/c.md"], "2099-02-01")
        self.command("append", "--entries", "-", stdin=json.dumps([old, skipped3, fresh]))
        out = self.command("auto-dismiss", "--today", "2099-02-05")
        self.assertEqual(sorted(d["id"] for d in out["auto_dismissed"]), ["old", "skipped"])
        by_id = {e["id"]: e for e in tomllib.loads(self.queue.read_text())["pending"]}
        self.assertEqual(by_id["old"]["status"], "auto-dismissed")
        self.assertEqual(by_id["fresh"]["status"], "pending")
        self.assertIn("dismiss_reason", by_id["skipped"])


class ResolutionTest(QueueCase):
    def test_resolve_anchors_dedupe_on_decision_date(self) -> None:
        self.command("append", "--entries", "-", stdin=json.dumps([_entry("a", ["wip/x.md", "wip/y.md"], "2099-01-01")]))
        out = self.command("resolve", "--id", "a", "--status", "dismissed", "--reason", "user skipped", "--today", "2099-03-01")
        self.assertEqual(out["status"], "dismissed")
        data = tomllib.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(data["pending"][0]["resolved_at"], "2099-03-01")
        self.assertEqual(data["pending"][0]["dismiss_reason"], "user skipped")
        out = self.command("append", "--entries", "-", "--today", "2099-03-21",
                       stdin=json.dumps([_entry("again", ["wip/y.md", "wip/x.md"], "2099-03-21")]))
        self.assertEqual(out["appended"], [])
        self.assertIn("a (dismissed)", out["skipped"][0]["reason"])
        out = self.command(
            "resolve", "--id", "a", "--status", "applied", "--reason", "merge is right",
            expected_status=1,
        )
        self.assertIn("error", out)

    def test_defer_increments_and_feeds_auto_dismiss(self) -> None:
        self.command("append", "--entries", "-", stdin=json.dumps([_entry("a", ["wip/x.md"], "2099-02-01")]))
        for day in ("2099-02-02", "2099-02-03", "2099-02-04"):
            out = self.command("defer", "--id", "a", "--today", day)
        self.assertEqual(out["surface_count"], 3)
        out = self.command("auto-dismiss", "--today", "2099-02-05")
        self.assertEqual([d["id"] for d in out["auto_dismissed"]], ["a"])



class AutoDismissDryRunTest(QueueCase):
    def test_dry_run_lists_candidates_without_writing(self) -> None:
        """/triage shows the housekeeping candidates before approving them."""
        self.command("append", "--entries", "-", stdin=json.dumps([
            _entry("old", ["wip/a.md"], proposed_at="2099-01-01"),
            _entry("fresh", ["wip/b.md"], proposed_at="2099-02-04"),
        ]))
        before = self.queue.read_text(encoding="utf-8")
        out = self.command("auto-dismiss", "--today", "2099-02-05", "--dry-run")
        self.assertTrue(out["dry_run"])
        self.assertEqual([d["id"] for d in out["auto_dismissed"]], ["old"])
        self.assertEqual(self.queue.read_text(encoding="utf-8"), before)
        statuses = {e["id"]: e["status"] for e in tomllib.loads(before)["pending"]}
        self.assertEqual(statuses["old"], "pending")

    def test_veto_dismissal_records_only_after_queue_write(self) -> None:
        args = ["--queue", "/unused", "veto-expired", "--today", "2099-01-02", "--apply-dismissals"]
        expected = {
            "render": (2, ["render"]),
            "write": (2, ["render", "queue"]),
            "success": (0, ["render", "queue", "ledger"]),
        }
        real_render = pending.render
        for failure, outcome in expected.items():
            events: list[str] = []
            data = {"schema_version": 1, "pending": [{
                "id": "review-only", "category": "redundant", "status": "pending",
                "default_action": "dismiss", "default_at": "2099-01-01",
            }]}

            def render(document: dict) -> str:
                events.append("render")
                if failure == "render":
                    raise ModuleNotFoundError("No module named 'tomli_w'")
                return real_render(document)

            def write(*_args: object) -> None:
                events.append("queue")
                if failure == "write":
                    raise OSError("fixture write failure")

            with mock.patch.object(pending, "load", return_value=data), \
                    mock.patch.object(pending, "render", side_effect=render), \
                    mock.patch.object(pending, "atomic_write", side_effect=write), \
                    mock.patch.object(pending.decisions, "record_best_effort", side_effect=lambda **_kw: events.append("ledger")), \
                    redirect_stdout(io.StringIO()):
                result = pending.main(args)
            self.assertEqual((result, events), outcome)



class DefaultWithVetoTest(QueueCase):
    """time-stale-A entries under wip/research get a 14-day default; nothing else does."""

    def _stale(self, eid: str, peers: list[str]) -> dict:
        entry = _entry(eid, peers)
        entry["category"] = "time-stale-A"
        entry["proposed_action"] = "close or redate the lapsed plan"
        return entry

    def test_append_stamps_default_only_for_eligible_peers(self) -> None:
        entries = [
            self._stale("e-wip", ["wip/plan.md"]),
            self._stale("e-refl", ["reflections/2099-01-01-reflection.md"]),
            _entry("e-redundant", ["wip/a.md", "wip/b.md"]),
        ]
        out = self.command("append", "--entries", "-", "--today", "2099-01-01", "--rule-defaults", stdin=json.dumps(entries))
        self.assertEqual(sorted(out["appended"]), ["e-redundant", "e-refl", "e-wip"])
        rows = {e["id"]: e for e in tomllib.loads(self.queue.read_text())["pending"]}
        self.assertEqual(rows["e-wip"]["default_action"], "stale-banner")
        self.assertEqual(rows["e-wip"]["default_at"], "2099-01-15")
        self.assertNotIn("default_at", rows["e-refl"])
        self.assertNotIn("default_at", rows["e-redundant"])

        before = self.command("veto-expired", "--today", "2099-01-14")
        self.assertEqual(before["expired"], [])
        due = self.command("veto-expired", "--today", "2099-01-15")
        self.assertEqual([e["id"] for e in due["expired"]], ["e-wip"])
        self.assertEqual(due["expired"][0]["peers"], ["wip/plan.md"])

        deferred = self.command("defer", "--id", "e-wip", "--today", "2099-01-10")
        self.assertEqual(deferred["default_at"], "2099-01-24")
        self.assertEqual(self.command("veto-expired", "--today", "2099-01-15")["expired"], [])

        vetoed = self.command("resolve", "--id", "e-wip", "--status", "dismissed",
                          "--reason", "user skipped", "--today", "2099-01-25")
        self.assertEqual(vetoed["status"], "dismissed")
        self.assertEqual(self.command("veto-expired", "--today", "2099-02-01")["expired"], [])

    def test_append_without_rule_flag_leaves_defaults_to_the_judge(self) -> None:
        self.command("append", "--entries", "-", "--today", "2099-01-01",
                 stdin=json.dumps([self._stale("e-wip", ["wip/plan.md"])]))
        rows = tomllib.loads(self.queue.read_text())["pending"]
        self.assertNotIn("default_action", rows[0])
        ledger = self.vault / "decisions.jsonl"
        out = self.command("--ledger", str(ledger), "set-default", "--id", "e-wip", "--action", "dismiss",
                       "--today", "2099-01-05", "--reason", "3 of 3 similar past entries were dismissed")
        self.assertEqual(out["default_at"], "2099-01-19")
        line = json.loads(ledger.read_text().splitlines()[-1])
        self.assertEqual((line["by"], line["verdict"], line["class"]), ("precedent", "dismiss", "autoevo/time-stale-A"))
        due = self.command("veto-expired", "--today", "2099-01-19", "--apply-dismissals")
        self.assertEqual(due["dismissed"], ["e-wip"])
        self.assertEqual(due["expired"], [])
        row = tomllib.loads(self.queue.read_text())["pending"][0]
        self.assertEqual((row["status"], row["dismiss_reason"]), ("dismissed", "default after veto window"))
        refused = self.command(
            "set-default", "--id", "e-wip", "--action", "dismiss", "--today", "2099-01-20",
            expected_status=1,
        )
        self.assertIn("not pending", refused["error"])

    def test_resolve_requires_a_reason_and_writes_the_ledger(self) -> None:
        self.command("append", "--entries", "-", "--today", "2099-01-01",
                 stdin=json.dumps([self._stale("e-wip", ["wip/plan.md"])]))
        proc = _raw(self.vault, self.queue, "resolve", "--id", "e-wip", "--status", "dismissed")
        self.assertEqual(proc.returncode, 2, "resolve without --reason must be an argparse error")
        ledger = self.vault / "decisions.jsonl"
        out = self.command("--ledger", str(ledger), "resolve", "--id", "e-wip", "--status", "dismissed",
                       "--reason", "plan is still active this quarter", "--today", "2099-01-03")
        self.assertEqual(out["status"], "dismissed")
        line = json.loads(ledger.read_text().splitlines()[-1])
        self.assertEqual(line["verdict"], "dismiss")
        self.assertEqual(line["reason"], "plan is still active this quarter")
        self.assertEqual(line["features"]["tier"], "wip")
        self.assertEqual(line["by"], "human")


if __name__ == "__main__":
    unittest.main()
