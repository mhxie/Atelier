"""Reading proposals cannot manufacture taste; policy results need real labels."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import decisions  # noqa: E402
import reading_feedback as reading  # noqa: E402


def event(kind="proposed", *, item="readwise:example", episode="session-1", by=None, policy=None):
    policy = policy or {"model": "test-model", "files": {"curate.md": "hash"}, "context": {}}
    if "sources" not in policy:
        policy = {**policy, "sources": policy["files"], "context_sources": policy["context"],
                  "files": {p: hashlib.sha256(text.encode()).hexdigest() for p, text in policy["files"].items()},
                  "context": {p: hashlib.sha256(text.encode()).hexdigest() for p, text in policy["context"].items()}}
    policy = {**policy, "id": reading.fingerprint({k: v for k, v in policy.items() if k != "id"})}
    f = {
        "schema": 1, "episode_id": episode, "item_id": item, "policy_id": policy["id"],
        "event_id": f"{episode}:{item}:{kind}", "evidence_ref": "session-1/turn-2",
    }
    if kind == "proposed":
        f.update(action="deep-read", item={"title": "An example", "summary": "Concrete examples",
                                          "category": "article"}, policy=policy)
    return {
        "class": reading.CLASS, "subject": reading.subject(episode, item), "verdict": kind,
        "by": by or ("agent" if kind in {"proposed", "shown"} else "human"),
        "reason": "Explicit evidence for this event", "features": f, "source": "test",
        "ts": "2099-01-01T00:00:00",
    }


class ReadingEvidenceTests(unittest.TestCase):
    def test_proposals_and_exposure_are_not_preference_evidence(self):
        rows = [event(), event("shown")]
        self.assertEqual(reading.evidence(rows)["explicit_feedback"], [])
        self.assertEqual(reading.evidence(rows)["consumption"], [])
        counts = next(iter(reading.outcomes(rows)["policies"].values()))
        self.assertEqual((counts["rated"], counts["unknown"], counts["candidate_useful_rate"]), (0, 1, None))

    def test_batch_approval_does_not_imply_consumption_or_usefulness(self):
        rows = [event(), event("approved")]
        counts = next(iter(reading.outcomes(rows)["policies"].values()))
        self.assertEqual((counts["approved"], counts["consumed"], counts["rated"]), (1, 0, 0))
        self.assertEqual(reading.dataset(rows)[0]["cases"], [])

    def test_consumption_is_separate_from_explicit_taste(self):
        rows = [event(), event("consumed", by="observed")]
        output = reading.evidence(rows)
        self.assertEqual(output["explicit_feedback"], [])
        self.assertEqual(output["consumption"][0]["by"], "observed")
        self.assertEqual(reading.dataset(rows)[1]["labels"], [])

    def test_negative_rating_and_reason_survive_and_replace_previous_rating(self):
        negative = event("not-useful")
        negative["reason"] = "Relevant subject, but the treatment was shallow"
        rows = [event(), event("useful"), negative, event("deferred")]
        ratings = [r for r in reading.evidence(rows)["explicit_feedback"] if r["event"] == "not-useful"]
        self.assertEqual(ratings[0]["reason"], negative["reason"])
        self.assertEqual(reading.dataset(rows)[1]["labels"][0]["useful"], False)

    def test_retry_deduplicates_and_conflicting_identity_fails_closed(self):
        rows = [event(), event("useful"), event("useful")]
        counts = next(iter(reading.outcomes(rows)["policies"].values()))
        self.assertEqual((counts["proposed"], counts["rated"]), (1, 1))
        corrupted = copy.deepcopy(rows[-1])
        corrupted["verdict"] = "not-useful"
        self.assertEqual(reading.evidence(rows + [corrupted])["explicit_feedback"], [])
        self.assertGreater(reading.outcomes(rows + [corrupted])["invalid_count"], 0)

    def test_orphan_is_explicit_evidence_but_not_a_policy_outcome(self):
        rows = [event("useful")]
        self.assertFalse(reading.evidence(rows)["explicit_feedback"][0]["attributed"])
        self.assertEqual(reading.outcomes(rows)["policies"], {})
        self.assertEqual(reading.dataset(rows)[0]["cases"], [])

    def test_mismatched_policy_cannot_receive_credit(self):
        rating = event("useful")
        rating["features"]["policy_id"] = "other-policy"
        self.assertEqual(reading.outcomes([event(), rating])["policies"], {})

    def test_forged_human_rating_is_rejected_on_write_and_read(self):
        forged = event("useful", by="agent")
        with self.assertRaises(ValueError):
            reading.validate(forged)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                decisions.record(cls=reading.CLASS, subject=forged["subject"], verdict="useful", by="agent",
                                 reason=forged["reason"], features=forged["features"], path=Path(tmp) / "d.jsonl")
            self.assertFalse((Path(tmp) / "d.jsonl").exists())
            with self.assertRaises(ValueError):
                decisions.record(cls=" reading/item ", subject=forged["subject"], verdict="useful", by="agent",
                                 reason=forged["reason"], features=forged["features"], path=Path(tmp) / "d.jsonl")
        self.assertEqual(reading.evidence([forged])["explicit_feedback"], [])

    def test_evidence_is_bounded_and_reports_omissions(self):
        rows = [event("useful", item=f"readwise:{i}") for i in range(4)]
        output = reading.evidence(rows, limit=2)
        self.assertEqual(len(output["explicit_feedback"]), 2)
        self.assertEqual(output["omitted"], 2)
        self.assertEqual(output["explicit_feedback"][-1]["item_id"], "readwise:3")

    def test_recent_correction_on_old_episode_survives_limit(self):
        rows = [event(item="a"), event("useful", item="a"), event(item="b"), event("useful", item="b"),
                event("not-useful", item="a")]
        result = reading.evidence(rows, limit=1)["explicit_feedback"]
        self.assertEqual((result[0]["item_id"], result[0]["event"]), ("a", "not-useful"))

    def test_lookup_finds_shown_episode_without_taste_feedback(self):
        rows = [event(), event("shown")]
        result = reading.attribution(rows, "readwise:example")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["episodes"][0]["episode_id"], "session-1")
        self.assertEqual(reading.evidence(rows)["explicit_feedback"], [])

    def test_archive_relevance_is_not_a_successful_read_recommendation(self):
        proposal = event()
        proposal["features"]["action"] = "archive"
        counts = next(iter(reading.outcomes([proposal, event("useful")])["policies"].values()))
        self.assertNotIn("deep-read", counts["by_action"])
        self.assertEqual(counts["by_action"]["archive"]["useful"], 1)

    def test_reading_actors_do_not_expand_legacy_cli_choices(self):
        self.assertEqual(decisions.BY_VALUES, ("human", "precedent", "rule"))

    def test_bare_explicit_feedback_can_be_recorded_without_explanation(self):
        row = event("useful")
        with tempfile.TemporaryDirectory() as tmp:
            recorded = decisions.record(cls=reading.CLASS, subject=row["subject"], verdict="useful",
                                        by="human", reason="有用", features=row["features"], path=Path(tmp) / "d.jsonl")
            self.assertEqual(reading.evidence([recorded])["explicit_feedback"][0]["reason"], "有用")

    def test_reading_feedback_does_not_expand_autoevo_authority(self):
        precedent = {"class": "autoevo/redundant", "by": "precedent", "ts": "2099-01-01", "subject": "a"}
        rating = event("useful")
        rating["ts"] = "2099-01-02"
        rows = [precedent, event(), event("consumed", by="observed"), rating]
        self.assertEqual(decisions.unconfirmed_since_heartbeat(rows, "autoevo/redundant"), 1)
        self.assertNotIn(reading.CLASS, decisions.precedent_stats(rows, date(2099, 1, 3)))

    def test_policy_fingerprint_tracks_worktree_model_and_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, context = Path(tmp) / "curate.md", Path(tmp) / "profile.md"
            policy.write_text("before")
            context.write_text("goal")
            first = reading.policy_snapshot([policy], [context], "model-a")
            policy.write_text("after")
            second = reading.policy_snapshot([policy], [context], "model-a")
            third = reading.policy_snapshot([policy], [context], "model-b")
            context.write_text("new goal")
            fourth = reading.policy_snapshot([policy], [context], "model-b")
            self.assertEqual(len({p["id"] for p in (first, second, third, fourth)}), 4)
            self.assertEqual(first["sources"][str(policy)], "before")
            self.assertEqual(first["context_sources"][str(context)], "goal")
            reading.validate_policy(first)


class ReadingEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [event(), event("useful"), event(item="readwise:noise"),
                     event("not-useful", item="readwise:noise"), event(item="readwise:unknown")]
        self.cases, self.labels = reading.dataset(self.rows)
        ids = [c["id"] for c in self.cases["cases"]]
        base_policy = event()["features"]["policy"]
        candidate_policy = event(policy={"model": "test-model", "files": {"curate.md": "new-hash"}, "context": {}})["features"]["policy"]
        self.baseline = {"case_set_id": self.cases["case_set_id"], "policy_id": base_policy["id"], "policy": base_policy,
                         "predictions": [{"id": key, "selected": True} for key in ids]}
        self.candidate = {"case_set_id": self.cases["case_set_id"], "policy_id": candidate_policy["id"], "policy": candidate_policy,
                          "predictions": [{"id": key, "selected": i == 0} for i, key in enumerate(ids)]}

    def test_case_export_hides_labels_and_prior_recommendations(self):
        self.assertEqual(len(self.cases["cases"]), 2)
        allowed = {"id", "item_id", *reading.ITEM_FIELDS}
        for case in self.cases["cases"]:
            self.assertLessEqual(set(case), allowed)
        self.assertEqual(self.cases["case_set_id"], self.labels["case_set_id"])

    def test_case_export_preserves_duration_tags_and_other_selection_inputs(self):
        proposal = event()
        proposal["features"]["item"].update(reading_time=120, word_count=2000, tags=["deep-read"], author="Example Author")
        cases, _ = reading.dataset([proposal, event("useful")])
        self.assertEqual(cases["cases"][0]["reading_time"], 120)
        self.assertEqual(cases["cases"][0]["tags"], ["deep-read"])

    def test_comparison_measures_noise_and_missed_useful_items(self):
        result = reading.compare(self.labels, self.baseline, self.candidate)
        self.assertEqual(result["baseline"]["precision"], 0.5)
        self.assertEqual(result["candidate"]["precision"], 1.0)
        self.assertEqual(result["candidate"]["useful_missed"], 0)
        self.assertEqual(result["decision"], "review_required")

    def test_empty_selection_has_unknown_precision_and_misses_useful_item(self):
        for row in self.candidate["predictions"]:
            row["selected"] = False
        result = reading.compare(self.labels, self.baseline, self.candidate)["candidate"]
        self.assertIsNone(result["precision"])
        self.assertEqual(result["useful_missed"], 1)

    def test_incomplete_duplicate_and_wrong_set_predictions_rejected(self):
        for mutation in ("missing", "duplicate", "wrong-set", "string-label", "same-policy"):
            candidate = copy.deepcopy(self.candidate)
            if mutation == "missing":
                candidate["predictions"].pop()
            elif mutation == "duplicate":
                candidate["predictions"].append(candidate["predictions"][0])
            elif mutation == "wrong-set":
                candidate["case_set_id"] = "another-set"
            elif mutation == "string-label":
                candidate["predictions"][0]["selected"] = "true"
            else:
                candidate["policy_id"] = self.baseline["policy_id"]
                candidate["policy"] = self.baseline["policy"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                reading.compare(self.labels, self.baseline, candidate)

    def test_correction_changes_dataset_identity(self):
        correction = event("not-useful")
        cases, _ = reading.dataset(self.rows + [correction])
        self.assertNotEqual(cases["case_set_id"], self.cases["case_set_id"])

    def test_unrated_and_invalid_data_cannot_claim_improvement(self):
        with self.assertRaises(ValueError):
            reading.compare(reading.dataset([event()])[1], self.baseline, self.candidate)
        self.labels["invalid_count"] = 1
        with self.assertRaises(ValueError):
            reading.compare(self.labels, self.baseline, self.candidate)

    def test_cli_records_then_exports_and_compares_without_touching_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            ledger = scratch / "decisions.jsonl"
            def run(*args):
                result = subprocess.run([sys.executable, str(ROOT / "scripts/decisions.py"),
                                         "--ledger", str(ledger), *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            for i, row in enumerate(self.rows):
                payload = {**row["features"], "event": row["verdict"], "by": row["by"], "reason": row["reason"]}
                path = scratch / f"input-{i}.json"
                path.write_text(json.dumps(payload))
                run("reading-record", "--input", str(path))
            self.assertEqual(len(run("reading-evidence")["explicit_feedback"]), 2)
            before = ledger.read_bytes()
            exported = run("reading-cases", "--view", "labels")
            for name, payload in (("labels", exported), ("baseline", self.baseline), ("candidate", self.candidate)):
                (scratch / f"{name}.json").write_text(json.dumps(payload))
            result = run("reading-evaluate", "--labels", str(scratch / "labels.json"),
                         "--baseline", str(scratch / "baseline.json"), "--candidate", str(scratch / "candidate.json"))
            self.assertEqual(result["candidate"]["noise_selected"], 0)
            self.assertEqual(before, ledger.read_bytes())


class ReadingStorageTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.ledger = self.root / "decisions.jsonl"
        self.policy = event()["features"]["policy"]

    def run_cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/decisions.py"),
                                 "--ledger", str(self.ledger), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stdout + result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def test_batch_stores_one_snapshot_and_returns_only_compact_receipt(self):
        policy = {"model": "test-model", "files": {"curate.md": "private policy text " * 1000}, "context": {}}
        rows = [event(item=f"readwise:{i}", policy=policy) for i in range(20)]
        receipt = decisions.record_reading(rows, self.ledger)
        self.assertEqual(receipt, {"recorded": 20, "duplicates": 0, "episodes": 20, "policies_recorded": 1})
        stored = decisions.load(self.ledger)
        self.assertEqual(sum(r["class"] == reading.POLICY_CLASS for r in stored), 1)
        self.assertTrue(all("policy" not in r["features"] for r in stored if r["class"] == reading.CLASS))
        self.assertNotIn("private policy text", json.dumps(receipt))
        self.assertLess(len(self.ledger.read_bytes()), len(json.dumps(rows).encode()) / 5)
        self.assertEqual(next(iter(reading.outcomes(stored)["policies"].values()))["unknown"], 20)
        self.assertIn("policy", rows[0]["features"], "the caller's input must not be mutated")

    def test_stable_id_retry_does_not_append_or_shift_evidence_recency(self):
        rows = [event(), event("useful"), event("useful", item="newer")]
        decisions.record_reading(rows, self.ledger)
        before = self.ledger.read_bytes()
        retry = copy.deepcopy(rows[:2])
        for row in retry:
            row["ts"] = "2099-02-01T00:00:00"
        result = decisions.record_reading(retry, self.ledger)
        self.assertEqual((result["recorded"], result["duplicates"], result["policies_recorded"]), (0, 2, 0))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(reading.evidence(rows + retry, limit=1)["explicit_feedback"][0]["item_id"], "newer")

    def test_conflicting_retry_rejects_entire_batch_before_appending_policy(self):
        decisions.record_reading([event(), event("useful")], self.ledger)
        before = self.ledger.read_bytes()
        conflict = event("useful")
        conflict["verdict"] = "not-useful"
        new = event(item="new", policy={"model": "new-model", "files": {"curate.md": "new"}, "context": {}})
        with self.assertRaisesRegex(ValueError, "conflicting event identity"):
            decisions.record_reading([new, conflict], self.ledger)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_invalid_legacy_event_is_not_acknowledged_as_a_successful_retry(self):
        broken = event()
        broken["features"]["policy"]["sources"]["curate.md"] = "tampered"
        self.ledger.write_text(json.dumps(broken) + "\n")
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(ValueError, "conflicting event identity"):
            decisions.record_reading([event()], self.ledger)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_interrupted_append_requires_repair_before_retry_or_export(self):
        rows = [event(), event("useful")]
        real_write = decisions.os.write
        calls = 0

        def interrupted_write(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                # Persist the policy and the beginning of the proposal, then
                # simulate a full disk or interrupted filesystem write.
                return real_write(fd, data[:bytes(data).index(b"\n") + 25])
            raise OSError("simulated interrupted append")

        with patch.object(decisions.os, "write", interrupted_write), self.assertRaises(OSError):
            decisions.record_reading(rows, self.ledger)
        damaged = self.ledger.read_bytes()
        self.assertFalse(damaged.endswith(b"\n"))
        with self.assertRaisesRegex(ValueError, "repair required"):
            decisions.record_reading(rows, self.ledger)
        self.assertEqual(self.ledger.read_bytes(), damaged, "retry must not join new events onto a torn tail")
        for args in (("reading-cases", "--view", "labels"), ("reading-outcomes",),
                     ("reading-policy", "--id", self.policy["id"])):
            error = self.run_cli(*args, ok=False)
            self.assertIn("repair required", error["error"])
        self.assertEqual(self.ledger.read_bytes(), damaged)

    def test_corrupt_complete_rows_also_block_reading_appends(self):
        for corruption in (b"{broken}\n", b"[]\n", b"null\n", b"\xe4\n"):
            with self.subTest(corruption=corruption):
                self.ledger.write_bytes((json.dumps(decisions.policy_record(self.policy)) + "\n").encode() + corruption)
                before = self.ledger.read_bytes()
                with self.assertRaisesRegex(ValueError, "repair required"):
                    decisions.record_reading([event(), event("useful")], self.ledger)
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_invalid_batch_never_partially_records_valid_feedback(self):
        decisions.record_reading([event()], self.ledger)
        before = self.ledger.read_bytes()
        for mutation in ("forged", "missing-policy", "schema", "extra-feature", "blank-id"):
            bad = event(item="bad")
            if mutation == "forged":
                bad = event("useful", by="agent")
            elif mutation == "missing-policy":
                del bad["features"]["policy"]
                bad["features"]["policy_id"] = "missing"
            elif mutation == "schema":
                bad["features"]["schema"] = True
            elif mutation == "extra-feature":
                bad["features"]["useful"] = True
            else:
                bad["features"]["event_id"] = ""
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                decisions.record_reading([event("useful"), bad], self.ledger)
            self.assertEqual(self.ledger.read_bytes(), before)

    def test_concurrent_batches_deduplicate_under_the_shared_lock(self):
        rows = [event(item=f"item:{i}") for i in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: decisions.record_reading(rows, self.ledger), range(8)))
        self.assertEqual(sum(r["recorded"] for r in receipts), 8)
        self.assertEqual(sum(r["policies_recorded"] for r in receipts), 1)
        self.assertEqual(len(decisions.load(self.ledger)), 9)

    def test_missing_or_corrupt_policy_cannot_receive_outcome_credit(self):
        proposal = event()
        del proposal["features"]["policy"]
        for records in ([], [decisions.policy_record(self.policy)]):
            if records:
                records[0]["features"]["policy"] = copy.deepcopy(self.policy)
                records[0]["features"]["policy"]["sources"]["curate.md"] = "tampered"
            rows = records + [proposal, event("useful")]
            self.assertEqual(reading.outcomes(rows)["policies"], {})
            self.assertGreater(reading.dataset(rows)[1]["invalid_count"], 0)
        # A valid copy alongside a conflicting record cannot rescue the identity.
        rows = [decisions.policy_record(self.policy), *records, proposal, event("useful")]
        self.assertEqual(reading.outcomes(rows)["policies"], {})

    def test_legacy_inline_policy_resolves_new_references_without_migration(self):
        legacy = event()
        self.ledger.write_text(json.dumps(legacy) + "\n")
        before = self.ledger.read_bytes()
        compact = event(item="second")
        del compact["features"]["policy"]
        result = decisions.record_reading([compact, event("useful", item="second")], self.ledger)
        self.assertEqual(result["policies_recorded"], 0)
        self.assertTrue(self.ledger.read_bytes().startswith(before))
        self.assertEqual(decisions.record_reading([legacy], self.ledger)["duplicates"], 1)
        output = self.run_cli("reading-evidence", "--item", "second")
        self.assertTrue(output["explicit_feedback"][0]["attributed"])
        self.assertEqual(self.run_cli("reading-policy", "--id", self.policy["id"]), self.policy)

    def test_frozen_private_export_preserves_policies_without_leaking_to_cases(self):
        legacy = [event(), event("useful")]
        decisions.record_reading(legacy, self.ledger)
        cases, labels = reading.dataset(decisions.load(self.ledger))
        self.assertEqual((cases, labels), reading.dataset(legacy))
        self.assertEqual(labels["policies"], {self.policy["id"]: self.policy})
        self.assertEqual(labels["attribution"][cases["cases"][0]["id"]], self.policy["id"])
        self.assertNotIn("policies", cases)
        self.assertNotIn("attribution", cases)

    def test_policy_capture_receipt_and_readonly_lookup(self):
        source, context = self.root / "curate.md", self.root / "context.md"
        source.write_text("exact private policy")
        context.write_text("exact private context")
        args = ("reading-policy", "--file", str(source), "--context", str(context), "--model", "test-model")
        receipt = self.run_cli(*args)
        self.assertEqual(set(receipt), {"id", "recorded", "duplicates"})
        self.assertEqual((receipt["recorded"], receipt["duplicates"]), (1, 0))
        before = self.ledger.read_bytes()
        self.assertEqual(self.run_cli(*args)["duplicates"], 1)
        source.unlink()
        snapshot = self.run_cli("reading-policy", "--id", receipt["id"])
        self.assertEqual(snapshot["sources"][str(source)], "exact private policy")
        self.assertEqual(snapshot["context_sources"][str(context)], "exact private context")
        self.assertEqual(self.ledger.read_bytes(), before)
        for extra in (("--file", str(source)), ("--context", str(context)), ("--model", "other")):
            self.run_cli("reading-policy", "--id", receipt["id"], *extra, ok=False)
            self.assertEqual(self.ledger.read_bytes(), before)

    def test_missing_policy_lookup_does_not_create_a_ledger(self):
        self.run_cli("reading-policy", "--id", "unknown", ok=False)
        self.assertFalse(self.ledger.exists())

    def test_policy_records_do_not_change_maintenance_stats_or_heartbeat(self):
        precedent = {"class": "autoevo/redundant", "by": "precedent", "ts": "2099-01-01", "subject": "a"}
        record = decisions.policy_record(self.policy)
        record.update(by="human", ts="2099-02-01")  # Even invalid imports cannot grant authority.
        rows = [precedent, record]
        self.assertEqual(decisions.unconfirmed_since_heartbeat(rows, "autoevo/redundant"), 1)
        self.assertNotIn(reading.POLICY_CLASS, decisions.precedent_stats(rows, date(2099, 2, 3)))
        with self.assertRaises(ValueError):
            decisions.record(cls=" reading/policy ", subject=record["subject"], verdict="captured", by="human",
                             reason=record["reason"], features=record["features"], path=self.ledger)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()
