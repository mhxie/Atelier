"""Fixture-only end-to-end tests for isolated proposal acceptance and recovery."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
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
import autoevo_commit as commits  # noqa: E402
import autoevo_pending as pending  # noqa: E402
import autoevo_preflight as preflight  # noqa: E402
import autoevo_run as run  # noqa: E402
import autoevo_verify as evidence  # noqa: E402
import decisions  # noqa: E402

CYCLE = "2099-01-20"
CLEAN_LINT = {"counts": {"error": 0, "warn": 0, "info": 0}, "findings": []}


class AutoevoTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="atelier-autoevo-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault, self.workspace = self.root / "vault", self.root / "workspace"
        for relative in ("wip", "research/lab", "reflections", "agent-findings", "archive", "_meta", "cache"):
            (self.vault / relative).mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir()
        (self.vault / ".gitignore").write_text("_meta/\ncache/\n")
        (self.vault / "personal.txt").write_text("untouched\n")
        (self.vault / "wip/seed.md").write_text("short original note\n")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.com")
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        self.old("wip/seed.md")
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

    def old(self, relative, days=500):
        stamp = (datetime.fromisoformat(CYCLE) - timedelta(days=days)).timestamp()
        os.utime(self.vault / relative, (stamp, stamp))

    def prepare(self):
        readiness = preflight.inspect_preflight(vault=self.vault)
        self.assertTrue(readiness["ready"], readiness)
        plan = run.prepare_workspace(self.vault, self.workspace, CYCLE, readiness)
        proposal = {"schema_version": 1, "cycle_id": CYCLE, "sweeps": [
            {"scope": dispatch["scope"], "outcome": "envelope_returned", "mode": "full",
             "completion_status": "complete", "remaining_work": "", "gaps": "", "findings": [], "notes": []}
            for dispatch in plan["dispatches"]], "judgments": {}, "notes": [], "errors": []}
        return plan, proposal

    def accept(self, plan, proposal, **kwargs):
        return run.accept_proposal(self.vault, proposal, plan, flow_run_id="fixture-flow",
                                   lint_check=kwargs.pop("lint_check", lambda _vault: CLEAN_LINT), **kwargs)

    def record(self):
        return evidence.read_record(evidence.record_path(self.vault, CYCLE))

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

    def test_empty_cycle_has_one_result_and_derived_verified_reports(self):
        plan, proposal = self.prepare()
        record = self.accept(plan, proposal)
        self.assertEqual(record["status"], "complete")
        self.assertEqual([op["kind"] for op in record["operations"]], ["queue", "audit"])
        self.assertTrue(evidence.verify_cycle(vault=self.vault, cycle=CYCLE)["verified"])
        self.assertEqual(run.prior_result(self.vault, CYCLE), self.record())
        self.assertFalse(evidence.record_path(self.vault, CYCLE).with_suffix(".toml").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(list((self.vault / "cache").iterdir()))
        for relative in record["plan"]["state_files"]:
            if (self.vault / relative).exists():
                self.assertEqual((self.vault / relative).stat().st_mode & 0o777, 0o600)

    def test_snapshot_excludes_dirty_content_and_keeps_original_mtime(self):
        (self.vault / "wip/seed.md").write_text("human edit\n")
        plan, _ = self.prepare()
        self.assertIn("wip/seed.md", plan["protected_paths"])
        self.assertNotIn("wip/seed.md", plan["source_files"])
        self.assertEqual([row["scope"] for row in plan["dispatches"]], ["wip", "research/lab", "reflections"])

    def test_archive_has_its_own_revertible_commit_and_preserves_other_edits(self):
        original = b"short original note\r\n"
        (self.vault / "wip/seed.md").write_bytes(original)
        (self.vault / "wip/seed.md").chmod(0o755)
        self.git("add", "wip/seed.md")
        self.git("commit", "-qm", "CRLF archive source")
        self.old("wip/seed.md")
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        (self.vault / "personal.txt").write_text("human staged\n")
        self.git("add", "personal.txt")
        (self.vault / "personal.txt").write_text("human unstaged\n")
        record = self.accept(plan, proposal)
        operation = record["operations"][0]
        self.assertEqual(operation["kind"], "low-signal-high")
        target = next(path for path in operation["changes"] if path.startswith("archive/"))
        self.assertEqual((self.vault / target).read_bytes(), original)
        self.assertEqual((self.vault / target).stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.vault / "wip/seed.md").exists())
        self.assertIn("MM personal.txt", self.git("status", "--porcelain"))
        self.assertEqual(self.git("show", ":personal.txt"), "human staged")
        self.git("restore", "--staged", "personal.txt")
        self.git("revert", "--no-edit", operation["commit"]["sha"])
        self.assertEqual((self.vault / "wip/seed.md").read_bytes(), original)
        self.assertIsNotNone(run.tombstone_reason(self.vault, ["wip/seed.md"], date.fromisoformat(CYCLE)))

    def test_source_change_after_proposal_refuses_without_overwriting_it(self):
        plan, proposal = self.prepare()
        row = self.archive(plan, proposal)
        (self.vault / "wip/seed.md").write_text("changed by human\n")
        self.old("wip/seed.md")
        row["curator"]["proposed_content"] = "changed by human\n"
        head = self.git("rev-parse", "HEAD")
        with self.assertRaises(evidence.VerificationError):
            self.accept(plan, proposal)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.vault / "wip/seed.md").read_text(), "changed by human\n")
        self.assertEqual(self.record()["status"], "failed")

    def test_queue_publication_cannot_gain_note_write_authority(self):
        plan, proposal = self.prepare()
        record = {"schema_version": 1, "plan": plan, "run_id": plan["run_id"], "operations": [],
                  "reports": {}, "output_file": "agent-findings/audit.md", "errors": []}
        changes = run._changes(self.vault, {"wip/seed.md": "unauthorized queue write\n"})
        with self.assertRaises(commits.PublicationError):
            run._publish(self.vault, record, evidence.record_path(self.vault, CYCLE), "queue", changes, "queue")
        self.assertFalse(record["operations"][-1]["publication_started"])
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
            self.old(f"wip/{name}.md", 500 + index)
        self.git("add", "wip")
        self.git("commit", "-qm", "merge fixture")
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

    def test_expired_default_commits_banner_queue_and_ledger_together(self):
        (self.vault / "research/lab/other.md").write_text("another old deadline\n")
        self.git("add", "research/lab/other.md")
        self.git("commit", "-qm", "second source")
        self.default(["wip/seed.md", "research/lab/other.md"])
        plan, proposal = self.prepare()
        record = self.accept(plan, proposal)
        operation = record["operations"][0]
        self.assertEqual(operation["kind"], "stale-banner")
        self.assertEqual(set(operation["changes"]), {"wip/seed.md", "research/lab/other.md", "_meta/autoevo_pending.toml", "_meta/decisions.jsonl"})
        self.assertIn("> Stale since " + CYCLE, (self.vault / "wip/seed.md").read_text())
        self.assertIn("> Stale since " + CYCLE, (self.vault / "research/lab/other.md").read_text())
        self.assertEqual(pending.load(self.vault / "_meta/autoevo_pending.toml")["pending"][0]["status"], "applied")
        for relative in ("_meta/autoevo_pending.toml", "_meta/decisions.jsonl"):
            self.assertEqual((self.vault / relative).stat().st_mode & 0o777, 0o600)
            self.assertTrue(self.git("ls-tree", "HEAD", "--", relative).startswith("100644 blob"))
        self.git("revert", "--no-edit", operation["commit"]["sha"])
        ledger = self.root / "undo-ledger.jsonl"
        run.record_undos(self.vault, ledger, date.fromisoformat(CYCLE))
        run.record_undos(self.vault, ledger, date.fromisoformat(CYCLE))
        self.assertEqual(len(decisions.load(ledger)), 1)
        self.assertEqual(decisions.load(ledger)[0]["verdict"], "undo")

    def test_veto_or_defer_after_plan_prevents_default_without_any_publication(self):
        self.default()
        plan, proposal = self.prepare()
        path = self.vault / "_meta/autoevo_pending.toml"
        path.write_text(path.read_text().replace('status = "pending"', 'status = "dismissed"'))
        head = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(evidence.VerificationError, "state changed"):
            self.accept(plan, proposal)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
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
        self.assertTrue(self.git("ls-tree", "HEAD", "--", "_meta/decisions.jsonl").startswith("100644 blob"))

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

    def test_new_lint_error_marks_committed_result_failed(self):
        plan, proposal = self.prepare()
        bad = {"counts": {"error": 1, "warn": 0, "info": 0}, "findings": [{"severity": "ERROR", "code": "fixture"}]}
        lint = mock.Mock(side_effect=[CLEAN_LINT, bad])
        record = self.accept(plan, proposal, lint_check=lint)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(len(record["lint"]["new_errors"]), 1)
        self.assertIn("introduced lint errors", record["errors"][-1])

    def test_write_failure_records_ambiguity_and_never_replays(self):
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        with mock.patch.object(commits, "_delete_file", side_effect=OSError("fixture interruption")):
            with self.assertRaises(commits.PublicationError):
                self.accept(plan, proposal)
        self.assertEqual(self.record()["status"], "needs_review")
        before = self.git("status", "--porcelain")
        with mock.patch.object(commits, "publish_changes") as publish:
            with self.assertRaises((evidence.VerificationError, commits.PublicationError)):
                run.prior_result(self.vault, CYCLE)
            publish.assert_not_called()
        self.assertEqual(self.git("status", "--porcelain"), before)

    def test_committed_final_audit_can_reconcile_only_missing_result_update(self):
        plan, proposal = self.prepare()
        record = self.accept(plan, proposal)
        expected = deepcopy(record)
        record["status"] = "publishing"
        del record["operations"][-1]["commit"]
        evidence.write_record(evidence.record_path(self.vault, CYCLE), record)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(run.prior_result(self.vault, CYCLE), expected)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        record = self.record()
        record["operations"][-1]["changes"][record["output_file"]]["after"] += "tampered\n"
        evidence.write_record(evidence.record_path(self.vault, CYCLE), record)
        with self.assertRaises(evidence.VerificationError):
            evidence.verify_cycle(vault=self.vault, cycle=CYCLE)

    def test_post_commit_proof_failure_remains_ambiguous_on_later_days(self):
        plan, proposal = self.prepare()
        self.archive(plan, proposal)
        before = self.git("rev-parse", "HEAD")
        with mock.patch.object(commits, "reconcile_commit", side_effect=commits.PublicationError("proof read failed", publication_started=False)):
            with self.assertRaises(commits.PublicationError) as raised:
                self.accept(plan, proposal)
        self.assertTrue(raised.exception.publication_started)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), before)
        record = self.record()
        self.assertEqual(record["status"], "needs_review")
        # A pre-fix record without an explicit no-effects proof also blocks.
        record["status"] = "failed"
        del record["operations"][-1]["publication_started"]
        evidence.write_record(evidence.record_path(self.vault, CYCLE), record)
        with mock.patch.object(commits, "publish_changes") as publish:
            with self.assertRaisesRegex(evidence.VerificationError, "needs review"):
                run.prior_result(self.vault, "2099-01-21")
            publish.assert_not_called()

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

    def test_historical_json_requires_recognized_state_and_operation_evidence(self):
        plan, proposal = self.prepare()
        complete = self.accept(plan, proposal)
        path = evidence.record_path(self.vault, CYCLE)
        later = "2099-01-21"
        self.assertIsNone(run.prior_result(self.vault, later))
        for mutation in (
            {"status": "unknown-after-crash"}, {"status": "prepared"}, {"status": []},
            {"cycle_id": "2099-01-19"}, {"operations": None}, {"operations": [{}]},
            {"errors": None}, {"status": "failed", "errors": []}, {"operations": []},
        ):
            with self.subTest(mutation=mutation):
                record = {**complete, **mutation}
                evidence.write_record(path, record)
                with self.assertRaises(evidence.VerificationError):
                    run.prior_result(self.vault, later)
        for field in ("operations", "errors"):
            record = deepcopy(complete)
            del record[field]
            evidence.write_record(path, record)
            with self.assertRaises(evidence.VerificationError):
                run.prior_result(self.vault, later)
        failed = {**complete, "status": "failed", "errors": ["pre-publication refusal"], "operations": []}
        evidence.write_record(path, failed)
        self.assertIsNone(run.prior_result(self.vault, later))
        with self.assertRaisesRegex(evidence.VerificationError, "no automatic replay"):
            run.prior_result(self.vault, CYCLE)

    def test_failed_historical_operations_need_exact_git_proof(self):
        plan, proposal = self.prepare()
        complete = self.accept(plan, proposal)
        failed = {**complete, "status": "failed", "errors": ["interrupted after a committed operation"]}
        path = evidence.record_path(self.vault, CYCLE)
        later = "2099-01-21"
        evidence.write_record(path, failed)
        self.assertIsNone(run.prior_result(self.vault, later))
        for changes in ({"commit": {}}, {"commit": None}, {"changes": {}}, {"expected_head": "0" * 40}):
            with self.subTest(changes=changes):
                broken = deepcopy(failed)
                broken["operations"][0].update(changes)
                evidence.write_record(path, broken)
                with self.assertRaises(evidence.VerificationError):
                    run.prior_result(self.vault, later)
        refused = deepcopy(failed)
        operation = refused["operations"].pop()
        del operation["commit"]
        operation["publication_started"] = False
        refused["operations"].append(operation)
        evidence.write_record(path, refused)
        self.assertIsNone(run.prior_result(self.vault, later))

    def test_head_advance_and_legacy_cycle_refuse_before_candidate_acceptance(self):
        plan, proposal = self.prepare()
        self.git("commit", "--allow-empty", "-qm", "human advance")
        with self.assertRaisesRegex(evidence.VerificationError, "HEAD changed"):
            self.accept(plan, proposal)
        path = evidence.record_path(self.vault, CYCLE).with_suffix(".toml")
        path.parent.mkdir(parents=True)
        path.write_text("legacy\n")
        with self.assertRaisesRegex(evidence.VerificationError, "legacy receipt"):
            run.prior_result(self.vault, CYCLE)

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


if __name__ == "__main__":
    unittest.main()
