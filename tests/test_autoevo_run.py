"""Fixture-only end-to-end tests for isolated proposal acceptance and recovery."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import _paths  # noqa: E402
import autoevo_pending as pending  # noqa: E402
import autoevo_preflight as preflight  # noqa: E402
import autoevo_run as run  # noqa: E402
import autoevo_verify as evidence  # noqa: E402
import decisions  # noqa: E402

CYCLE, NEXT = "2099-01-20", "2099-01-21"
NOW = datetime.fromisoformat(CYCLE).timestamp()
CLEAN_LINT = {"counts": {"error": 0, "warn": 0, "info": 0}, "findings": []}
READ_ONLY_GIT = {"rev-parse", "ls-tree", "hash-object"}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class AutoevoTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="atelier-autoevo-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault, self.workspace = self.root / "vault", self.root / "workspace"
        for relative in ("wip", "research/lab", "reflections", "agent-findings", "archive", "_meta", "cache"):
            (self.vault / relative).mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir()
        (self.vault / ".gitignore").write_text("cache/\n_meta/*\n!_meta/autoevo_*.toml\n!_meta/decisions.jsonl\n")
        (self.vault / "personal.txt").write_text("untouched\n")
        (self.vault / "wip/seed.md").write_text("short original note\n")
        for days in (100, 200, 300, 400):  # a staggered history keeps mtime ages meaningful
            (self.vault / f"reflections/r{days}.md").write_text(f"reflection {days}\n")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.com")
        self.reflect()
        self.old("wip/seed.md")
        for days in (100, 200, 300, 400):
            self.old(f"reflections/r{days}.md", days)
        paths = tomllib.loads((ROOT / "harness/paths.toml").read_text())["paths"]
        for patch in (
            mock.patch.dict(os.environ, {"OV": str(self.vault), "PYTHONDONTWRITEBYTECODE": "1"}),
            mock.patch.object(_paths, "_registry", return_value=paths),
            mock.patch.object(preflight, "_default_privacy_probe", return_value={"hit_count": 0}),
            mock.patch.object(preflight, "_default_semantic_probe", return_value={"ready": True, "mode": "qmd"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.vault), *args], capture_output=True,
                              text=True, check=True, timeout=30).stdout.strip()

    def reflect(self):
        """What Reflect does after a change: stage everything and commit it."""
        self.git("add", "-A")
        self.git("commit", "-qm", "Update notes")

    def old(self, relative, days=500):
        stamp = (datetime.fromisoformat(CYCLE) - timedelta(days=days)).timestamp()
        os.utime(self.vault / relative, (stamp, stamp))

    def prepare(self, cycle=CYCLE):
        readiness = preflight.inspect_preflight(vault=self.vault)
        self.assertTrue(readiness["ready"], readiness)
        plan = run.prepare_workspace(self.vault, self.workspace, cycle, readiness, now=NOW)
        proposal = {"schema_version": 1, "cycle_id": cycle, "sweeps": [
            {"scope": dispatch["scope"], "outcome": "envelope_returned", "mode": "full",
             "completion_status": "complete", "remaining_work": "", "gaps": "", "findings": [], "notes": []}
            for dispatch in plan["dispatches"]], "judgments": {}, "notes": [], "errors": []}
        return plan, proposal

    def accept(self, plan, proposal, **kwargs):
        return run.accept_proposal(self.vault, proposal, plan, flow_run_id="fixture-flow",
                                   lint_check=kwargs.pop("lint_check", lambda _vault: CLEAN_LINT), **kwargs)

    def read_only_git(self, action):
        calls, original = [], subprocess.run

        def recorded(argv, *args, **kwargs):
            if argv and argv[0] == "git":
                calls.append(argv)
            return original(argv, *args, **kwargs)

        with mock.patch("subprocess.run", side_effect=recorded):
            result = action()
        self.assertLessEqual({next(arg for arg in argv[1:] if not arg.startswith("-")) for argv in calls}, READ_ONLY_GIT)
        self.assertFalse(any("-w" in argv for argv in calls))
        return result

    def record(self):
        return evidence.read_record(evidence.record_path(self.vault, CYCLE))

    def states(self):
        return evidence.verify_operations(self.vault, self.record())

    def archive(self, plan, proposal):
        row = {"category": "low-signal", "candidate": "wip/seed.md", "confidence": "high",
               "conditions_met": 5, "evidence": "short, no tags or inbound links, old working note",
               "proposed_action": "archive", "curator": {
                   "mode": "auto-apply", "band": "low-signal-high", "operation": "archive",
                   "completion_status": "complete", "remaining_work": "", "auto_apply_safe": True,
                   "source_path": "wip/seed.md",
                   "snapshot_paths": [plan["source_files"]["wip/seed.md"]["snapshot"]],
                   "proposed_content": (self.vault / "wip/seed.md").read_bytes().decode("utf-8")}}
        proposal["sweeps"][0]["findings"].append(row)
        return row

    def default(self, peers=None):
        entry = {"id": "due-item", "category": "time-stale-A", "peers": peers or ["wip/seed.md"],
                 "proposed_action": "mark stale", "evidence_summary": "finish by last summer",
                 "proposed_at": "2099-01-01", "status": "pending",
                 "default_action": "stale-banner", "default_at": "2099-01-15"}
        path = self.vault / "_meta/autoevo_pending.toml"
        pending.atomic_write(path, pending.render({"schema_version": 1, "pending": [entry]}))
        return entry

    def test_empty_cycle_keeps_a_verified_receipt_without_visible_notes(self):
        plan, proposal = self.prepare()
        head = self.git("rev-parse", "HEAD")
        record = self.read_only_git(lambda: self.accept(plan, proposal))
        self.assertEqual(record["status"], "complete")
        self.assertEqual([(op["kind"], op["state"]) for op in record["operations"]], [("queue", "applied")])
        self.assertEqual(record["output_file"], evidence.record_path(self.vault, CYCLE).relative_to(self.vault).as_posix())
        self.assertEqual(record["reports"], {})
        self.assertFalse(list((self.vault / "agent-findings").iterdir()))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(set(evidence.verify_cycle(vault=self.vault, cycle=CYCLE)["operations"].values()), {"pending-commit"})
        self.reflect()
        self.assertEqual(set(evidence.verify_cycle(vault=self.vault, cycle=CYCLE)["operations"].values()), {"committed"})
        self.assertEqual(run.prior_result(self.vault, CYCLE), self.record())
        self.assertFalse(evidence.record_path(self.vault, CYCLE).with_suffix(".toml").exists())
        self.assertFalse(list((self.vault / "cache").iterdir()))
        for relative in record["plan"]["state_files"]:
            if (self.vault / relative).exists():
                self.assertEqual((self.vault / relative).stat().st_mode & 0o777, 0o600)

    def test_fresh_pending_finding_publishes_once_and_unchanged_pending_does_not(self):
        plan, proposal = self.prepare()
        finding = self.archive(plan, proposal)
        finding["category"] = "time-stale-A"
        record = self.accept(plan, proposal)
        self.assertEqual(len(record["pending"]), 1)
        report = self.vault / record["output_file"]
        self.assertTrue(report.is_file())
        self.assertIn(record["pending"][0], report.read_text())
        for detail in ("wip/seed.md", finding["evidence"], finding["proposed_action"]):
            self.assertIn(detail, report.read_text())
        self.assertEqual(record["reports"], {})
        self.assertEqual(list((self.vault / "agent-findings").iterdir()), [report])
        self.assertTrue(evidence.verify_cycle(vault=self.vault, cycle=CYCLE)["verified"])
        plan, proposal = self.prepare(NEXT)
        self.archive(plan, proposal)["category"] = "time-stale-A"
        repeated = self.accept(plan, proposal)
        self.assertEqual(repeated["pending"], [])
        self.assertEqual(list((self.vault / "agent-findings").iterdir()), [report])
        self.assertTrue(evidence.verify_cycle(vault=self.vault, cycle=NEXT)["verified"])

    def test_coverage_failure_without_findings_keeps_only_machine_evidence(self):
        plan, proposal = self.prepare()
        proposal["sweeps"][1].update(outcome="forgetter_no_envelope", mode="absent",
                                      completion_status="aborted", remaining_work="worker interrupted")
        record = self.accept(plan, proposal)
        self.assertEqual(record["status"], "failed")
        self.assertTrue((self.vault / record["output_file"]).is_file())
        self.assertFalse(list((self.vault / "agent-findings").iterdir()))
        with self.assertRaises(evidence.VerificationError):
            evidence.verify_cycle(vault=self.vault, cycle=CYCLE)

    def test_actionable_result_cannot_hide_its_report_behind_a_machine_receipt(self):
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        record = self.accept(plan, proposal)
        self.assertTrue((self.vault / record["output_file"]).is_file())
        path = evidence.record_path(self.vault, CYCLE)
        record["output_file"] = path.relative_to(self.vault).as_posix()
        record["operations"] = [op for op in record["operations"] if op["kind"] != "audit"]
        evidence.write_record(path, record)
        with self.assertRaisesRegex(evidence.VerificationError, "cannot omit"):
            evidence.verify_cycle(vault=self.vault, cycle=CYCLE)

    def test_applied_evolution_still_publishes_if_post_write_lint_raises(self):
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        lint = mock.Mock(side_effect=[CLEAN_LINT, RuntimeError("lint unavailable")])
        with self.assertRaisesRegex(RuntimeError, "lint unavailable"):
            self.accept(plan, proposal, lint_check=lint)
        record = self.record()
        report = self.vault / record["output_file"]
        self.assertEqual(record["status"], "failed")
        self.assertTrue(report.is_file())
        self.assertIn("lint unavailable", report.read_text())
        self.assertIn("low-signal-high applied", report.read_text())
        self.assertFalse((self.vault / "wip/seed.md").exists())

    def test_newly_armed_existing_decision_publishes_evidence_and_veto_deadline(self):
        entry = self.default()
        del entry["default_action"], entry["default_at"]
        queue = self.vault / "_meta/autoevo_pending.toml"
        pending.atomic_write(queue, pending.render({"schema_version": 1, "pending": [entry]}))
        plan, proposal = self.prepare()
        preview = run.preview(self.vault, proposal, plan, self.workspace / "preview")
        proposal["judgments"][entry["id"]] = {
            "bundle_sha256": preview["bundles"][entry["id"]]["bundle_sha256"],
            "judgment": {"verdict": "apply", "confidence": 1, "cited": [0, 1, 2]}}
        with mock.patch.object(run.precedent, "gate", return_value={
                "default": True, "verdict": "apply", "cited": [0, 1, 2], "reason": "fixture precedent"}):
            record = self.accept(plan, proposal)
        self.assertEqual(record["pending"], [entry["id"]])
        report = (self.vault / record["output_file"]).read_text()
        for detail in (entry["evidence_summary"], entry["proposed_action"], "2099-02-03", "stale-banner"):
            self.assertIn(detail, report)
        self.assertTrue(evidence.verify_cycle(vault=self.vault, cycle=CYCLE)["verified"])

    def test_interrupted_queue_mutation_publishes_review_and_blocks_later_cycles(self):
        entry = self.default()
        entry["default_action"] = "dismiss"
        queue = self.vault / "_meta/autoevo_pending.toml"
        pending.atomic_write(queue, pending.render({"schema_version": 1, "pending": [entry]}))
        plan, proposal = self.prepare()
        original_write = run.atomic_write

        def interrupted(path, *args, **kwargs):
            if path == self.vault / "_meta/autoevo_quarantine.toml":
                raise OSError("queue mutation interrupted")
            return original_write(path, *args, **kwargs)

        with mock.patch.object(run, "atomic_write", interrupted), self.assertRaises(OSError):
            self.accept(plan, proposal)
        record = self.record()
        self.assertEqual(record["status"], "needs_review")
        self.assertIn("queue mutation interrupted", (self.vault / record["output_file"]).read_text())
        self.assertEqual(pending.load(queue)["pending"][0]["status"], "dismissed")
        with self.assertRaisesRegex(evidence.VerificationError, "half-applied"):
            run.prior_result(self.vault, NEXT)

    def test_pending_json_survives_prefect_print_logging(self):
        from prefect.context import FlowRunContext, TaskRunContext
        from prefect.logging.loggers import patch_print
        from types import SimpleNamespace

        plan, proposal = self.prepare()
        with mock.patch.object(FlowRunContext, "get", return_value=SimpleNamespace(log_prints=True)), \
                mock.patch.object(TaskRunContext, "get", return_value=None), \
                mock.patch("prefect.logging.loggers.get_run_logger"), patch_print():
            record = self.accept(plan, proposal)
        self.assertEqual(record["status"], "complete")
        self.assertTrue(evidence.verify_cycle(vault=self.vault, cycle=CYCLE)["verified"])

    def test_only_committed_conflict_free_settled_notes_become_sources(self):
        notes = {"wip/dirty.md": "committed\n", "wip/kept.md": "settled\n", "wip/recent.md": "fresh\n",
                 "wip/conflicted.md": "<<<<<<< ours\nmine\n=======\ntheirs\n>>>>>>> theirs\n"}
        for relative, body in notes.items():
            (self.vault / relative).write_text(body)
        self.reflect()
        (self.vault / "wip/dirty.md").write_text("uncommitted edit\n")
        (self.vault / "wip/untracked.md").write_text("never committed\n")
        for relative in ("wip/dirty.md", "wip/kept.md", "wip/conflicted.md", "wip/untracked.md"):
            self.old(relative)
        os.utime(self.vault / "wip/recent.md", (NOW - 60, NOW - 60))
        plan, _ = self.prepare()
        self.assertEqual(plan["protected_paths"], ["wip/conflicted.md", "wip/dirty.md", "wip/recent.md"])
        self.assertEqual(sorted(plan["source_files"]), ["reflections/r100.md", "reflections/r200.md", "reflections/r300.md",
                                                        "reflections/r400.md", "wip/kept.md", "wip/seed.md"])
        self.assertEqual(plan["source_files"]["wip/kept.md"]["before_blob"], self.git("rev-parse", "HEAD:wip/kept.md"))
        self.assertIsNone(plan["age_guard"])
        self.assertEqual([row["scope"] for row in plan["dispatches"]], ["wip", "research/lab", "reflections"])

    def test_mass_mtime_reset_turns_off_age_bands_and_is_receipted(self):
        for path in [*(self.vault / "reflections").glob("*.md"), self.vault / "wip/seed.md"]:
            os.utime(path, (NOW - 500 * 86400, NOW - 500 * 86400))  # one clone, long ago
        plan, proposal = self.prepare()
        self.assertTrue(plan["age_guard"].startswith("mtime_reset: 5 of 5"))
        row = self.archive(plan, proposal)
        today = date.fromisoformat(CYCLE)
        self.assertEqual(run.route_row(self.vault, row, today)[0], "auto_apply")
        self.assertEqual(run.route_row(self.vault, row, today, plan["age_guard"]), ("invalid", "low-signal", plan["age_guard"]))
        record = self.accept(plan, proposal)
        self.assertEqual([op["kind"] for op in record["operations"]], ["queue"])
        self.assertTrue((self.vault / "wip/seed.md").is_file())
        self.assertIn(plan["age_guard"], self.record()["plan"]["notes"])

    def test_archive_writes_files_only_and_a_plain_git_restore_is_a_veto(self):
        original = b"short original note\r\n"
        (self.vault / "wip/seed.md").write_bytes(original)
        self.reflect()
        self.old("wip/seed.md")
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        (self.vault / "personal.txt").write_text("human staged\n")
        self.git("add", "personal.txt")
        (self.vault / "personal.txt").write_text("human unstaged\n")
        head = self.git("rev-parse", "HEAD")
        operation = self.read_only_git(lambda: self.accept(plan, proposal))["operations"][0]
        self.assertEqual((operation["kind"], operation["state"]), ("low-signal-high", "applied"))
        target = next(path for path in operation["paths"] if path.startswith("archive/"))
        self.assertEqual((self.vault / target).read_bytes(), original)
        self.assertFalse((self.vault / "wip/seed.md").exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("show", ":personal.txt"), "human staged")
        self.assertEqual(operation["paths"]["wip/seed.md"]["before_blob"], self.git("rev-parse", "HEAD:wip/seed.md"))
        self.assertEqual(operation["paths"][target]["after_blob"], self.git("hash-object", "--no-filters", target))
        self.assertEqual(self.states()[operation["candidate_id"]], "pending-commit")
        self.reflect()
        self.assertEqual(self.states()[operation["candidate_id"]], "committed")
        self.git("restore", f"--source={head}", "--", "wip/seed.md", target)
        self.assertEqual((self.vault / "wip/seed.md").read_bytes(), original)
        self.assertEqual(self.states()[operation["candidate_id"]], "reverted")
        self.assertIsNotNone(run.tombstone_reason(self.vault, ["wip/seed.md"], date.fromisoformat(NEXT)))
        self.assertIsNone(run.tombstone_reason(self.vault, ["wip/seed.md"], date.fromisoformat(CYCLE)))

    def test_archived_source_revived_by_a_sync_merge_is_queued_for_review(self):
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        operation = self.accept(plan, proposal)["operations"][0]
        self.reflect()
        (self.vault / "wip/seed.md").write_text("edited on the phone before the archive synced\n")
        self.assertEqual(self.states()[operation["candidate_id"]], "superseded")
        entries = run._revived_archives(self.vault, date.fromisoformat(NEXT), "next-run")
        self.assertEqual([entry["peers"] for entry in entries], [sorted(operation["paths"])])
        self.assertEqual(pending.validate_entry(entries[0]), [])
        (self.vault / next(rel for rel in operation["paths"] if rel.startswith("archive/"))).unlink()
        self.assertEqual(run._revived_archives(self.vault, date.fromisoformat(NEXT), "next-run"), [])

    def test_source_change_after_proposal_skips_without_overwriting_it(self):
        plan, proposal = self.prepare()
        row = self.archive(plan, proposal)
        (self.vault / "wip/seed.md").write_text("changed by human\n")
        self.old("wip/seed.md")
        row["curator"]["proposed_content"] = "changed by human\n"
        head = self.git("rev-parse", "HEAD")
        record = self.accept(plan, proposal)
        self.assertEqual(record["status"], "complete")
        self.assertEqual([op["kind"] for op in record["operations"]], ["queue"])
        self.assertTrue(any(note.startswith("skipped low-signal-high: wip/seed.md differs") for note in record["notes"]))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.vault / "wip/seed.md").read_text(), "changed by human\n")

    def test_write_skips_a_source_whose_bytes_are_no_longer_its_head_blob(self):
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        seed = self.vault / "wip/seed.md"
        original = seed.read_text()
        seed.write_text("committed on another device\n")
        self.reflect()
        seed.write_text(original)  # the snapshot's bytes again, but no longer what HEAD holds
        self.old("wip/seed.md")
        record = self.accept(plan, proposal)
        self.assertEqual([op["kind"] for op in record["operations"]], ["queue"])
        self.assertTrue(any("not committed at HEAD" in note for note in record["notes"]))
        self.assertEqual(seed.read_text(), original)

    def test_queue_write_cannot_gain_note_write_authority(self):
        plan, _ = self.prepare()
        record = {"plan": plan, "run_id": plan["run_id"], "operations": [], "notes": []}
        with self.assertRaisesRegex(evidence.VerificationError, "may not write"):
            run._apply(self.vault, record, evidence.record_path(self.vault, CYCLE), "queue",
                       {"wip/seed.md": "unauthorized queue write\n"}, {"wip/seed.md": None})
        self.assertEqual(record["operations"], [])
        self.assertEqual((self.vault / "wip/seed.md").read_text(), "short original note\n")

    def test_low_signal_claims_are_recomputed_and_qmd_cannot_authorize_merge(self):
        plan, proposal = self.prepare()
        row = self.archive(plan, proposal)
        for body in ("word " * 200, "intentional #keep\n"):
            (self.vault / "wip/seed.md").write_text(body)
            self.old("wip/seed.md")
            self.assertEqual(run.route_row(self.vault, row, date.fromisoformat(CYCLE))[0], "invalid")
        row = {"category": "redundant", "candidate": "wip/a.md", "confidence": "high",
               "mode": "qmd", "peers": ["wip/b.md", "wip/c.md", "wip/d.md"], "scores": [1, 1, 1]}
        self.assertEqual(run.route_row(self.vault, row, date.fromisoformat(CYCLE))[0], "pending")
        row["mode"], row["scores"] = "real", [float("nan")] * 3
        self.assertEqual(run.route_row(self.vault, row, date.fromisoformat(CYCLE))[0], "invalid")

    def test_merge_needs_complete_curator_and_preserves_every_source(self):
        for index, name in enumerate(("a", "b", "c", "d")):
            (self.vault / f"wip/{name}.md").write_text(f"material {name}\n")
        self.reflect()
        for index, name in enumerate(("a", "b", "c", "d")):
            self.old(f"wip/{name}.md", 500 + index)
        plan, proposal = self.prepare()
        sources = [f"wip/{name}.md" for name in ("a", "b", "c", "d")]
        row = {"category": "redundant", "candidate": sources[0], "confidence": "high",
               "peers": sources[1:], "scores": [0.9] * 3, "mode": "real", "evidence": "fixture calibrated evidence",
               "proposed_action": "merge", "curator": {
                   "operation": "merge", "mode": "auto-apply", "band": "redundant-high",
                   "completion_status": "complete", "remaining_work": "", "auto_apply_safe": True,
                   "snapshot_paths": [plan["source_files"][rel]["snapshot"] for rel in sources],
                   "target_path": sources[-1], "proposed_content": "missing source material\n",
                   "media_inventory": {}, "media_output_count": {},
                   "content_integrity": dict.fromkeys(("verbatim_preserved", "structures_preserved", "images_preserved", "checklist_passed"), True)}}
        proposal["sweeps"][0]["findings"].append(row)
        self.assertEqual(len(run.route_proposal(self.vault, proposal, plan)[0]), 0)
        row["curator"]["proposed_content"] = "".join((self.vault / rel).read_text() for rel in sources)
        self.assertEqual(len(run.route_proposal(self.vault, proposal, plan)[0]), 0, "QMD provenance cannot be relabeled by a model")
        plan["retrieval_mode"] = "real"  # Explicit trusted fixture for the retained calibrated band.
        record = self.accept(plan, proposal)
        self.assertEqual(record["operations"][0]["kind"], "redundant-high")
        self.assertEqual({rel for rel in sources if (self.vault / rel).exists()}, {sources[-1]})

    def test_expired_default_writes_banners_and_a_restore_records_one_undo(self):
        (self.vault / "research/lab/other.md").write_text("another old deadline\n")
        self.reflect()
        self.default(["wip/seed.md", "research/lab/other.md"])
        plan, proposal = self.prepare()
        record = self.accept(plan, proposal)
        operation = record["operations"][0]
        self.assertEqual((operation["kind"], operation["entry"]), ("stale-banner", "due-item"))
        self.assertEqual(set(operation["paths"]), {"wip/seed.md", "research/lab/other.md"})
        self.assertIn("> Stale since " + CYCLE, (self.vault / "wip/seed.md").read_text())
        self.assertIn("> Stale since " + CYCLE, (self.vault / "research/lab/other.md").read_text())
        self.assertEqual(pending.load(self.vault / "_meta/autoevo_pending.toml")["pending"][0]["status"], "applied")
        for relative in ("_meta/autoevo_pending.toml", "_meta/decisions.jsonl"):
            self.assertEqual((self.vault / relative).stat().st_mode & 0o777, 0o600)
        head = self.git("rev-parse", "HEAD")
        self.reflect()
        self.git("restore", f"--source={head}", "--", "wip/seed.md", "research/lab/other.md")
        ledger = self.root / "undo-ledger.jsonl"
        run.record_undos(self.vault, ledger, date.fromisoformat(NEXT))
        run.record_undos(self.vault, ledger, date.fromisoformat(NEXT))
        self.assertEqual([(row["class"], row["subject"], row["verdict"]) for row in decisions.load(ledger)],
                         [("autoevo/time-stale-A", "due-item", "undo")])

    def test_veto_or_defer_after_plan_prevents_default_without_any_write(self):
        self.default()
        plan, proposal = self.prepare()
        path = self.vault / "_meta/autoevo_pending.toml"
        path.write_text(path.read_text().replace('status = "pending"', 'status = "dismissed"'))
        with self.assertRaisesRegex(evidence.VerificationError, "state changed"):
            self.accept(plan, proposal)
        self.assertNotIn("Stale", (self.vault / "wip/seed.md").read_text())
        self.assertFalse(evidence.record_path(self.vault, CYCLE).exists())

    def test_existing_private_ledger_keeps_permissions_and_rows(self):
        self.default()
        ledger = self.vault / "_meta/decisions.jsonl"
        original = decisions.record(cls="fixture", subject="seed", verdict="defer",
                                    reason="Preserve this existing private decision.", path=ledger)
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        plan, proposal = self.prepare()
        self.assertEqual(self.accept(plan, proposal)["status"], "complete")
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(decisions.load(ledger)[0], original)

    def test_missing_sweep_produces_diagnostic_audit_not_note_mutation(self):
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        proposal["sweeps"][1].update(outcome="forgetter_no_envelope", mode="absent",
                                      completion_status="aborted", remaining_work="worker interrupted")
        record = self.accept(plan, proposal)
        self.assertEqual(record["status"], "failed")
        self.assertTrue((self.vault / "wip/seed.md").is_file())
        self.assertEqual(len(record["pending"]), 1)
        with self.assertRaises(evidence.VerificationError):
            evidence.verify_cycle(vault=self.vault, cycle=CYCLE)

    def test_new_lint_error_marks_written_result_failed_and_publishes_triage(self):
        plan, proposal = self.prepare()
        bad = {"counts": {"error": 1, "warn": 0, "info": 0}, "findings": [{"severity": "ERROR", "code": "fixture"}]}
        lint = mock.Mock(side_effect=[CLEAN_LINT, bad])
        record = self.accept(plan, proposal, lint_check=lint)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(len(record["lint"]["new_errors"]), 1)
        self.assertIn("introduced lint errors", record["errors"][-1])
        self.assertTrue((self.vault / record["output_file"]).is_file())
        self.assertIn("introduced lint errors", (self.vault / record["output_file"]).read_text())

    def test_interrupted_write_is_half_applied_and_blocks_until_resolved(self):
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        seed, unlink = self.vault / "wip/seed.md", Path.unlink

        def interrupted(path, *args, **kwargs):
            if path == seed:
                raise OSError("fixture interruption")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", interrupted), self.assertRaises(OSError):
            self.accept(plan, proposal)
        operation = self.record()["operations"][0]
        self.assertEqual((self.record()["status"], operation["state"]), ("needs_review", "applying"))
        self.assertIn("fixture interruption", (self.vault / self.record()["output_file"]).read_text())
        for cycle in (CYCLE, NEXT):
            with self.assertRaisesRegex(evidence.VerificationError, "half-applied"):
                run.prior_result(self.vault, cycle)
        (self.vault / next(rel for rel in operation["paths"] if rel.startswith("archive/"))).unlink()
        self.assertEqual(self.states()[operation["candidate_id"]], "skipped")
        self.assertIsNone(run.prior_result(self.vault, NEXT))
        with self.assertRaisesRegex(evidence.VerificationError, "no automatic replay"):
            run.prior_result(self.vault, CYCLE)

    def test_operation_states_follow_live_content(self):
        head, target = self.git("rev-parse", "HEAD"), self.vault / "wip/seed.md"
        before, after = target.read_text(), "rewritten note\n"

        def operation(state="applied", **extra):
            paths = {"wip/seed.md": {"before_blob": None, "before_sha256": sha256(before), "after_blob": None, "after_sha256": sha256(after)},
                     **extra}
            return {"kind": "redundant-high", "candidate_id": "fixture", "state": state, "paths": paths}

        target.write_text(after)
        self.assertEqual(evidence.operation_state(self.vault, operation()), "pending-commit")
        self.reflect()
        self.assertEqual(evidence.operation_state(self.vault, operation()), "committed")
        added = {"wip/new.md": {"before_blob": None, "before_sha256": None, "after_blob": None, "after_sha256": sha256("new\n")}}
        self.assertEqual(evidence.operation_state(self.vault, operation(**added)), "superseded")
        self.assertEqual(evidence.operation_state(self.vault, operation("applying", **added)), "half-applied")
        target.write_text("a later user edit\n")
        self.assertEqual(evidence.operation_state(self.vault, operation()), "superseded")
        self.git("restore", f"--source={head}", "--", "wip/seed.md")
        self.assertEqual(evidence.operation_state(self.vault, operation()), "reverted")
        self.assertEqual(evidence.operation_state(self.vault, operation("applying")), "skipped")
        refused = {"kind": "queue", "candidate_id": "old", "publication_started": False,
                   "changes": {"wip/seed.md": {"before_sha256": sha256("unrelated\n"), "after": "x\n", "mode": 420}}}
        self.assertEqual(evidence.operation_state(self.vault, refused), "skipped")
        for broken in ({}, {**operation(), "paths": {}}, {**operation(), "kind": None},
                       {**operation(), "paths": {"wip/seed.md": {"before_sha256": "bad", "after_sha256": None}}},
                       {**operation(), "paths": {"../escape.md": operation()["paths"]["wip/seed.md"]}}):
            self.assertEqual(evidence.operation_state(self.vault, broken), "malformed")

    def test_historical_legacy_receipts_require_known_closed_state(self):
        path = evidence.record_path(self.vault, "2099-01-19").with_suffix(".toml")
        path.parent.mkdir(parents=True)
        header = 'contract_version = 3\nroutine = "autoevo-nightly"\ncycle_id = "2099-01-19"\n'
        for verification in ("pending", "failed", "passed", "blocked"):
            path.write_text(header + f'verification = "{verification}"\n')
            if verification in {"passed", "blocked"}:
                self.assertIsNone(run.prior_result(self.vault, CYCLE))
            else:
                with self.assertRaisesRegex(evidence.VerificationError, "legacy receipt"):
                    run.prior_result(self.vault, CYCLE)

    def test_commit_era_receipt_verifies_from_content_after_its_commits_are_gone(self):
        record = json.loads((ROOT / "tests/fixtures/autoevo_commit_era_receipt.json").read_text())
        for operation in record["operations"]:
            for relative, change in operation["changes"].items():
                if change["after"] is None:
                    (self.vault / relative).unlink(missing_ok=True)
                else:
                    (self.vault / relative).parent.mkdir(parents=True, exist_ok=True)
                    (self.vault / relative).write_text(change["after"])
        self.reflect()  # Reflect committed the content; the receipt's bot commits never existed here
        queue = self.vault / "_meta/autoevo_pending.toml"
        queue.write_text(queue.read_text() + "# a later review\n")
        evidence.write_record(evidence.record_path(self.vault, CYCLE), record)
        self.assertIsNone(run.prior_result(self.vault, NEXT))
        states = evidence.verify_cycle(vault=self.vault, cycle=CYCLE)["operations"]
        self.assertEqual([states[op["candidate_id"]] for op in record["operations"]],
                         ["committed", "superseded", "committed", "committed"])

    def test_historical_json_requires_a_recognized_state(self):
        self.default()
        plan, proposal = self.prepare()
        complete = self.accept(plan, proposal)
        path = evidence.record_path(self.vault, CYCLE)
        self.assertIsNone(run.prior_result(self.vault, NEXT))
        for mutation in (
            {"status": "unknown-after-crash"}, {"status": []}, {"cycle_id": "2099-01-19"},
            {"operations": None}, {"operations": [{}]}, {"errors": None},
            {"status": "failed", "errors": []}, {"operations": []},
        ):
            with self.subTest(mutation=mutation):
                evidence.write_record(path, {**complete, **mutation})
                with self.assertRaises(evidence.VerificationError):
                    run.prior_result(self.vault, NEXT)
        for field in ("operations", "errors"):
            record = deepcopy(complete)
            del record[field]
            evidence.write_record(path, record)
            with self.assertRaises(evidence.VerificationError):
                run.prior_result(self.vault, NEXT)
        for interrupted in ({"status": "prepared"}, {"status": "needs_review"},
                            {"status": "failed", "errors": ["pre-write refusal"], "operations": []}):
            with self.subTest(interrupted=interrupted):
                evidence.write_record(path, {**complete, **interrupted})
                self.assertIsNone(run.prior_result(self.vault, NEXT))
                with self.assertRaisesRegex(evidence.VerificationError, "no automatic replay"):
                    run.prior_result(self.vault, CYCLE)

    def test_only_half_applied_or_malformed_operations_block_later_cycles(self):
        self.default()
        plan, proposal = self.prepare()
        failed = {**self.accept(plan, proposal), "status": "failed", "errors": ["interrupted after a write"]}
        path = evidence.record_path(self.vault, CYCLE)
        for change in ({"paths": {}}, {"kind": None}, {"candidate_id": 7}, {"state": "applying", "paths": {
                "agent-findings/missing.md": {"before_sha256": None, "after_sha256": sha256("x\n")},
                failed["output_file"]: failed["operations"][-1]["paths"][failed["output_file"]]}}):
            with self.subTest(change=change):
                broken = deepcopy(failed)
                broken["operations"][-1].update(change)
                evidence.write_record(path, broken)
                with self.assertRaisesRegex(evidence.VerificationError, "half-applied|malformed"):
                    run.prior_result(self.vault, NEXT)
        (self.vault / failed["output_file"]).write_text("a user edit to the derived report\n")
        evidence.write_record(path, failed)
        self.assertIsNone(run.prior_result(self.vault, NEXT))
        self.assertEqual(self.states()[failed["operations"][-1]["candidate_id"]], "superseded")

    def test_head_may_advance_between_plan_and_write(self):
        plan, proposal = self.prepare()
        self.git("commit", "--allow-empty", "-qm", "Reflect sync from another device")
        record = self.accept(plan, proposal)
        self.assertEqual(record["status"], "complete")
        self.assertNotEqual(record["plan"]["base_head"], self.git("rev-parse", "HEAD"))
        path = evidence.record_path(self.vault, NEXT).with_suffix(".toml")
        path.write_text("legacy\n")
        with self.assertRaisesRegex(evidence.VerificationError, "legacy receipt"):
            run.prior_result(self.vault, NEXT)

    def test_preview_does_not_trust_model_queue_edits_or_stale_judgments(self):
        plan, proposal = self.prepare()
        proposal["sweeps"][0]["findings"].append({
            "category": "time-stale-A", "candidate": "wip/seed.md", "confidence": "medium",
            "evidence": "finish by last summer", "proposed_action": "mark stale"})
        preview = run.preview(self.vault, proposal, plan, self.workspace / "preview")
        key = next(iter(preview["bundles"]))
        self.assertFalse((self.vault / "_meta/autoevo_pending.toml").exists())
        (self.workspace / "preview/_meta/autoevo_pending.toml").write_text("malicious queue rewrite\n")
        proposal["judgments"][key] = {"bundle_sha256": "0" * 64,
                                     "judgment": {"verdict": "apply", "confidence": 1, "cited": [0, 1, 2]}}
        record = self.accept(plan, proposal)
        queued = pending.load(self.vault / "_meta/autoevo_pending.toml")["pending"]
        self.assertEqual(len(queued), 1)
        self.assertNotIn("default_action", queued[0])
        self.assertTrue(any("stale or unknown" in note for note in record["notes"]))

    def test_banner_preserves_frontmatter_and_heading(self):
        text = "---\ntitle: Example\n---\n# Example\n\nBody\n"
        banner = run.stale_banner_text(CYCLE, "entry", "old date")
        actual = run.insert_stale_banner(text, banner)
        self.assertTrue(actual.startswith("---\ntitle: Example\n---\n# Example\n"))
        self.assertIn(banner, actual)
        self.assertTrue(actual.endswith("Body\n"))

    def test_cluster_hash_is_order_insensitive_and_stable(self):
        self.assertEqual(run.cluster_hash(["wip/b.md", "wip/a.md"]), run.cluster_hash(["wip/a.md", "wip/b.md", "wip/a.md"]))
        self.assertEqual(run.cluster_hash(["wip/a.md"]), hashlib.sha1(b"wip/a.md\n").hexdigest()[:12])


if __name__ == "__main__":
    unittest.main()
