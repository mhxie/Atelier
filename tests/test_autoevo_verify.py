"""Structured Autoevo evidence tests; no Git, live vault, or model calls."""

from __future__ import annotations

from copy import deepcopy
import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import autoevo_verify as verify


def fixture() -> tuple[dict, dict]:
    plan = {
        "cycle_id": "2099-01-03",
        "dispatches": [{"scope": scope} for scope in ("wip", "research/topic", "reflections")],
        "quarantine_skipped": [], "notes": ["rotation selected"],
    }
    proposal = {
        "schema_version": verify.VERSION, "cycle_id": plan["cycle_id"],
        "sweeps": [
            {"scope": row["scope"], "outcome": "envelope_returned", "mode": "full",
             "completion_status": "complete", "remaining_work": "", "gaps": "",
             "findings": [], "notes": []}
            for row in plan["dispatches"]
        ],
        "judgments": {}, "notes": [], "errors": [],
    }
    return plan, proposal


def finding() -> dict:
    return {
        "category": "low-signal", "candidate": "wip/note.md", "confidence": "high",
        "evidence": "fixture source observations", "proposed_action": "archive",
        "conditions_met": 5,
    }


class ProposalContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan, self.proposal = fixture()

    def test_full_and_bounded_partial_are_valid_coverage(self) -> None:
        sweep = self.proposal["sweeps"][0]
        sweep.update(mode="partial", completion_status="partial", remaining_work="later candidates")
        sweep["notes"] = ["forgetter_partial: max_candidates"]
        before = deepcopy(self.proposal)
        self.assertEqual(verify.validate_proposal(self.proposal, self.plan), before)
        self.assertEqual(verify.coverage_errors(self.proposal, self.plan), [])
        self.assertEqual(self.proposal, before)

    def test_missing_repeated_or_unplanned_sweeps_are_rejected(self) -> None:
        variants = [
            self.proposal["sweeps"][:-1],
            [*self.proposal["sweeps"], self.proposal["sweeps"][0]],
            [{**row, "scope": "wiki"} if i == 0 else row for i, row in enumerate(self.proposal["sweeps"])],
        ]
        for sweeps in variants:
            with self.subTest(sweeps=sweeps), self.assertRaises(verify.VerificationError):
                verify.validate_proposal({**self.proposal, "sweeps": sweeps}, self.plan)

    def test_cycle_version_and_extra_authority_are_rejected(self) -> None:
        for extra in ({"cycle_id": "2099-01-04"}, {"schema_version": 999}, {"authorized_paths": ["wiki"]}):
            with self.subTest(extra=extra), self.assertRaises(verify.VerificationError):
                verify.validate_proposal({**self.proposal, **extra}, self.plan)

    def test_inconsistent_completion_metadata_is_rejected(self) -> None:
        for fields in (
            {"mode": "partial"}, {"completion_status": "aborted"},
            {"remaining_work": "unfinished"}, {"mode": "absent"},
        ):
            proposal = deepcopy(self.proposal)
            proposal["sweeps"][0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(verify.VerificationError):
                verify.validate_proposal(proposal, self.plan)

    def test_missing_envelope_has_no_accepted_findings(self) -> None:
        sweep = self.proposal["sweeps"][0]
        sweep.update(outcome="forgetter_no_envelope", mode="absent",
                     completion_status="aborted", remaining_work="scope unfinished")
        verify.validate_proposal(self.proposal, self.plan)
        self.assertTrue(verify.coverage_errors(self.proposal, self.plan))
        sweep["findings"] = [finding()]
        with self.assertRaises(verify.VerificationError):
            verify.validate_proposal(self.proposal, self.plan)

    def test_required_finding_evidence_cannot_be_omitted(self) -> None:
        for field in ("category", "candidate", "confidence", "evidence", "proposed_action"):
            row = finding()
            del row[field]
            self.proposal["sweeps"][0]["findings"] = [row]
            with self.subTest(field=field), self.assertRaises(verify.VerificationError):
                verify.validate_proposal(self.proposal, self.plan)

    def test_candidate_and_peer_paths_cannot_escape(self) -> None:
        for bad in ("/outside.md", "../outside.md", "wip/../outside.md", ".git/config",
                    "wip//note.md", "wip/./note.md", "wip\\\\note.md", "wip/line\n.md"):
            for field in ("candidate", "peers"):
                row = finding()
                row[field] = [bad] if field == "peers" else bad
                self.proposal["sweeps"][0]["findings"] = [row]
                with self.subTest(path=bad, field=field), self.assertRaises(verify.VerificationError):
                    verify.validate_proposal(self.proposal, self.plan)

    def test_judgments_require_a_digest_and_object(self) -> None:
        valid = {"bundle_sha256": "a" * 64, "judgment": {"verdict": "human"}}
        self.proposal["judgments"] = {"entry": valid}
        verify.validate_proposal(self.proposal, self.plan)
        for supplied in ({"judgment": {}}, {**valid, "bundle_sha256": "bad"},
                         {**valid, "judgment": "apply"}, {**valid, "authorize": True}):
            self.proposal["judgments"] = {"entry": supplied}
            with self.subTest(supplied=supplied), self.assertRaises(verify.VerificationError):
                verify.validate_proposal(self.proposal, self.plan)

    def test_coverage_retains_errors_and_quarantine_without_mutation(self) -> None:
        self.proposal["errors"] = ["fixture failure"]
        self.plan["quarantine_skipped"] = ["scope_quarantined: research/other"]
        before = deepcopy((self.proposal, self.plan))
        issues = verify.coverage_errors(self.proposal, self.plan)
        self.assertIn("fixture failure", issues)
        self.assertIn("scope_quarantined: research/other", issues)
        self.assertEqual((self.proposal, self.plan), before)


class DerivedReportTest(unittest.TestCase):
    def test_audit_is_deterministic_and_does_not_modify_record(self) -> None:
        plan, proposal = fixture()
        proposal["notes"] = ["本地提案"]
        record = {
            "cycle_id": plan["cycle_id"], "run_id": "fixture-run", "plan": plan,
            "proposal": proposal, "operations": [{"kind": "archive", "commit": {"sha": "abc123"}}],
            "pending": ["pending-id"], "lint": {"counts": {"error": 0, "warn": 1, "info": 2}},
            "notes": ["trusted-parent note"], "errors": [],
        }
        before = deepcopy(record)
        rendered = verify.render_report(record)
        self.assertEqual(rendered, verify.render_report(json.loads(json.dumps(record, sort_keys=True))))
        for value in ("fixture-run", "abc123", "pending-id", "本地提案", "trusted-parent note"):
            self.assertIn(value, rendered)
        self.assertEqual(record, before)

    def test_sweep_report_is_stable_across_record_serialization(self) -> None:
        _, proposal = fixture()
        sweep = proposal["sweeps"][0]
        sweep["findings"] = [finding()]
        self.assertEqual(
            verify.sweep_report(sweep),
            verify.sweep_report(json.loads(json.dumps(sweep, sort_keys=True))),
        )


class ResultInputTest(unittest.TestCase):
    def test_record_round_trip_and_transient_read(self) -> None:
        record = {"schema_version": verify.VERSION, "cycle_id": "2099-01-03", "notes": ["本地"]}
        with tempfile.TemporaryDirectory(prefix="atelier-result-") as tmp:
            path = Path(tmp).resolve() / "result.json"
            verify.write_record(path, record)
            self.assertEqual(verify.read_record(path), record)
            text = path.read_text()
            with mock.patch.object(Path, "read_text", side_effect=[
                OSError(errno.EDEADLK, "fixture materializing"), text,
            ]), mock.patch("_paths.time.sleep"):
                self.assertEqual(verify.read_record(path), record)
            self.assertEqual(path.read_text(), text)

    def test_missing_malformed_and_unsupported_records_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-result-") as tmp:
            path = Path(tmp).resolve() / "result.json"
            with self.assertRaises(verify.VerificationError):
                verify.read_record(path)
            for contents in ("{bad json", "[]", "null", "{}", '{"schema_version": 999}'):
                path.write_text(contents)
                with self.subTest(contents=contents), self.assertRaises(verify.VerificationError):
                    verify.read_record(path)
                self.assertEqual(path.read_text(), contents)

    def test_record_path_uses_safe_registry_and_calendar_date(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-result-") as tmp:
            vault = Path(tmp).resolve()
            with mock.patch.object(verify, "tier_segments", return_value={"meta": "metadata"}):
                self.assertEqual(
                    verify.record_path(vault, "2099-01-03"),
                    vault / "metadata/routine_receipts/autoevo-nightly/2099-01-03.json",
                )
                for cycle in ("not-a-date", "2099-1-03", "2099-02-31", "../2099-01-03"):
                    with self.subTest(cycle=cycle), self.assertRaises((verify.VerificationError, ValueError)):
                        verify.record_path(vault, cycle)

    def test_record_path_refuses_registry_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-result-") as tmp:
            for meta in ("../escape", "/outside", ".git", "nested/../escape"):
                with mock.patch.object(verify, "tier_segments", return_value={"meta": meta}):
                    with self.subTest(meta=meta), self.assertRaises(verify.VerificationError):
                        verify.record_path(Path(tmp).resolve(), "2099-01-03")

    def test_record_path_refuses_symlinked_parent_or_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-result-") as tmp:
            vault, outside = Path(tmp).resolve() / "vault", Path(tmp).resolve() / "outside"
            vault.mkdir()
            outside.mkdir()
            with mock.patch.object(verify, "tier_segments", return_value={"meta": "_meta"}):
                meta = vault / "_meta"
                meta.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(verify.VerificationError):
                    verify.record_path(vault, "2099-01-03")
                meta.unlink()
                parent = meta / "routine_receipts/autoevo-nightly"
                parent.mkdir(parents=True)
                target = parent / "2099-01-03.json"
                target.symlink_to(outside / "missing.json")
                with self.assertRaises(verify.VerificationError):
                    verify.record_path(vault, "2099-01-03")


if __name__ == "__main__":
    unittest.main()
