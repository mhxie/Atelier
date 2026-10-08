"""Cue escalation, failure surfacing, and ownership filtering tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
import cues  # noqa: E402

PRELUDE = """
import json, sys
sys.path.insert(0, 'scripts')
import cues
from datetime import date, datetime
from pathlib import Path
vault = Path(__import__('os').environ['OV'])
"""


def _run_py(vault: Path, body: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", PRELUDE + textwrap.dedent(body)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "OV": str(vault),
            "ATELIER_SKIP_LOCK_TOUCH": "1",
            "ATELIER_PREFECT_API_URL": "http://127.0.0.1:9/api",
            "PREFECT_API_URL": "http://127.0.0.1:9/api",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _autoevo_run(day: int, state_name: str, message: str = "", hour: int = 5) -> dict:
    state = "COMPLETED" if state_name == "Completed" else "FAILED"
    return {
        "routine": "autoevo-nightly",
        "state": state,
        "state_name": state_name,
        "message": message,
        "expected_start_time": datetime(2099, 1, day, hour, tzinfo=ZoneInfo("UTC")),
        "start_time": datetime(2099, 1, day, hour, 1, tzinfo=ZoneInfo("UTC")),
    }


def _autoevo_result(vault: Path, day: str) -> Path:
    path = cues.autoevo_verify.record_path(vault, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version": 1}\n', encoding="utf-8")
    return path


class AutoevoCueTest(unittest.TestCase):
    TODAY = date(2099, 1, 5)
    NOW = datetime(2099, 1, 5, 12, 0, tzinfo=ZoneInfo("UTC"))

    def _check(self, vault: Path, runs: list[dict]) -> tuple[cues.Cue | None, str]:
        with mock.patch.object(cues.routine_status, "recent_runs", return_value=runs):
            return cues.check_autoevo_ran(vault, self.TODAY, now=self.NOW)

    def test_same_not_ready_gate_three_days_escalates_from_prefect_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            runs = [
                _autoevo_run(day, "NotReady", '{"gate":"session_lock_unsafe"}')
                for day in (5, 4, 3)
            ]
            cue, debug = self._check(vault, runs)
            self.assertEqual(cue.severity, "hard")
            self.assertIn("3 consecutive days", cue.message)
            self.assertIn("symlinked session lock", cue.message)
            self.assertIn("streak=3", debug)

    def test_specific_fix_text_for_each_gate(self) -> None:
        for gate, expect in (
            ("privacy_hits", "privacy check"),
            ("semantic_unavailable", "semantic index"),
        ):
            with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
                vault = Path(tmp)
                runs = [_autoevo_run(day, "Deferred", f"autoevo deferred by {gate}") for day in (5, 4, 3)]
                cue, _debug = self._check(vault, runs)
                self.assertEqual(cue.severity, "hard")
                self.assertIn(expect, cue.message)
                self.assertNotIn("audit file", cue.message)

    def test_two_days_stays_soft(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            runs = [_autoevo_run(day, "NotReady", '{"gate":"session_active"}') for day in (5, 4)]
            cue, _debug = self._check(vault, runs)
            self.assertEqual(cue.severity, "soft")

    def test_newer_completion_supersedes_same_day_not_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            _autoevo_result(vault, "2099-01-05")
            runs = [
                _autoevo_run(5, "Completed", hour=6),
                _autoevo_run(5, "NotReady", '{"gate":"session_active"}', hour=5),
            ]
            with mock.patch.object(cues.routine_status, "recent_runs", return_value=runs), mock.patch.object(
                cues.autoevo_verify, "verify_cycle", return_value={"verified": True, "operations": {"abc": "committed"}}
            ):
                cue, debug = cues.check_autoevo_ran(vault, self.TODAY, now=self.NOW)
            self.assertIsNone(cue)
            self.assertIn("structured result verified", debug)

    def test_completed_run_requires_json_not_legacy_toml(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            legacy = cues.autoevo_verify.record_path(vault, "2099-01-05").with_suffix(".toml")
            legacy.parent.mkdir(parents=True)
            legacy.write_text("verification = 'passed'\n", encoding="utf-8")
            cue, debug = self._check(vault, [_autoevo_run(5, "Completed")])
            self.assertEqual(cue.severity, "soft")
            self.assertIn("structured JSON result is missing", cue.message)
            self.assertIn(".json", debug)

    def test_invalid_structured_result_surfaces_verifier_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            _autoevo_result(vault, "2099-01-05")
            with mock.patch.object(cues.routine_status, "recent_runs", return_value=[_autoevo_run(5, "Completed")]), mock.patch.object(
                cues.autoevo_verify, "verify_cycle", side_effect=cues.autoevo_verify.VerificationError("commit mismatch")
            ):
                cue, debug = cues.check_autoevo_ran(vault, self.TODAY, now=self.NOW)
            self.assertEqual(cue.severity, "soft")
            self.assertIn("not verified", cue.message)
            self.assertIn("commit mismatch", debug)

    def test_prefect_unavailable_is_silent_until_installation_evidence_exists(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            unavailable = cues.routine_status.StatusUnavailable("offline")
            with mock.patch.object(cues.routine_status, "recent_runs", side_effect=unavailable):
                cue, debug = cues.check_autoevo_ran(vault, self.TODAY, now=self.NOW)
                self.assertIsNone(cue)
                self.assertIn("no Autoevo installation evidence", debug)
                _autoevo_result(vault, "2099-01-04")
                cue, debug = cues.check_autoevo_ran(vault, self.TODAY, now=self.NOW)
            self.assertEqual(cue.severity, "soft")
            self.assertIn("status is unavailable", cue.message)
            self.assertIn("offline", debug)

    def test_before_six_is_silent_without_querying_prefect(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp, mock.patch.object(
            cues.routine_status, "recent_runs"
        ) as recent:
            cue, debug = cues.check_autoevo_ran(
                Path(tmp), self.TODAY, now=datetime(2099, 1, 5, 5, 59, tzinfo=ZoneInfo("UTC"))
            )
        self.assertIsNone(cue)
        self.assertIn("before 06:00", debug)
        recent.assert_not_called()


class ReflectIntakeCueTest(unittest.TestCase):
    def _check(self, vault: Path) -> dict:
        return _run_py(
            vault,
            """
            cue, debug = cues.check_reflect_intake(vault, date(2099, 1, 10))
            print(json.dumps({"key": cue.key if cue else None, "count": cue.count if cue else None, "items": cue.items if cue else None, "debug": debug}))
            """,
        )

    def test_counts_unfiled_notes_but_not_reflect_hubs_or_transcripts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            (vault / "notes").mkdir()
            (vault / "audio-memos").mkdir()
            for name in ("sample.md", "links.md", "audio-memos.md", "memo-2099-01-09.md"):
                (vault / "notes" / name).write_text("# note\n", encoding="utf-8")
            (vault / "audio-memos" / "memo-2099-01-09.m4a").write_bytes(b"")
            out = self._check(vault)
            self.assertEqual((out["key"], out["count"], out["items"]), ("reflect_intake", 1, ["sample.md"]), out)

    def test_missing_notes_folder_is_silent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            out = self._check(Path(tmp))
            self.assertIsNone(out["key"], out)


class IntentMissCueTest(unittest.TestCase):
    @staticmethod
    def _route(vault: Path, day: str, raw: str, kind: str = "general") -> None:
        routes = vault / "_meta" / "intent_routes"
        routes.mkdir(parents=True, exist_ok=True)
        with (routes / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"raw_input": raw, "match_kind": kind, "timestamp": f"{day}T09:00:00"}) + "\n")

    def _check(self, vault: Path) -> dict:
        return _run_py(
            vault,
            """
            cue, debug = cues.check_intent_misses(vault, date(2099, 1, 10))
            print(json.dumps({"key": cue.key if cue else None, "message": cue.message if cue else "", "debug": debug}))
            """,
        )

    def test_phrase_on_three_days_fires(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            for day in ("2099-01-03", "2099-01-05", "2099-01-08"):
                self._route(vault, day, "Plan my week")
            out = self._check(vault)
            self.assertEqual(out["key"], "intent_misses", out)
            self.assertIn("1 个", out["message"])

    def test_one_day_burst_stays_silent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            for i in range(6):
                self._route(vault, "2099-01-09", f"one-off request {i}")
            out = self._check(vault)
            self.assertIsNone(out["key"], out)
            self.assertIn("6 unrouted", out["debug"])


class WikiAttentionCueTest(unittest.TestCase):
    def test_flagged_reviews_and_expired_evidence_surface_until_settled(self) -> None:
        fence = ("```anchors c1\n@anchor: doi:fixture | valid_at: 2020-01-01{expiry}\n"
                 "@pass: editor | status: pending | at: 2020-02-01\n@pass: reviewer | status: {status} | at: 2020-03-01\n{extra}```\n")
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp, \
                mock.patch.object(cues, "tier_segments", return_value={}):
            vault = Path(tmp)
            note = vault / "wiki/topic/Entry.md"
            note.parent.mkdir(parents=True)
            body = "# Entry\n\nIntro <!-- claim:c1 -->a bounded claim.<!-- /claim:c1 -->\n\n## References\n\n"
            doubt = "@pass: reader | status: flagged | at: 2020-04-01\n"
            for status, expiry, extra, expected in (("flagged", "", "", ["[[Entry#^c1]] flagged by reviewer"]),
                                                    ("verified", "", doubt, ["[[Entry#^c1]] flagged by reader"]),
                                                    ("verified", " | invalid_at: 2020-06-01", "", ["[[Entry#^c1]] evidence expired"]),
                                                    ("verified", "", "", None)):
                note.write_text(body + fence.format(status=status, expiry=expiry, extra=extra), encoding="utf-8")
                cue, _ = cues.check_wiki_attention(vault, date.today())
                self.assertEqual(cue.items if cue else None, expected)


class RoutineCueTest(unittest.TestCase):
    def test_oldest_unreviewed_output_gets_a_visible_slot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            registry_dir = vault / "_tools/routines"
            registry_dir.mkdir(parents=True)
            rows = []
            for index, day in enumerate(("28", "27", "26", "01"), start=1):
                output_dir = f"reports/{index}"
                rows.append(
                    f'[[routine]]\noutput_dir = "{output_dir}"\n'
                    f'file_pattern = "report-*.md"\nlabel = "routine {index}"\n'
                )
                report_dir = vault / output_dir
                report_dir.mkdir(parents=True)
                (report_dir / f"report-2026-08-{day}.md").write_text("ok\n")
            (registry_dir / "registry.toml").write_text("version = 1\n\n" + "\n".join(rows))

            out = _run_py(
                vault,
                """
                cue, _ = cues.check_routine_outputs(vault, date(2026, 8, 28))
                print(json.dumps({"message": cue.message if cue else ""}))
                """,
            )

            self.assertIn("routine 4 (report-2026-08-01.md)", out["message"])
            self.assertNotIn("routine 1 (report-2026-08-28.md)", out["message"])


class CueErrorSurfacingTest(unittest.TestCase):
    def test_crashed_checks_are_logged_and_reported(self) -> None:
        import cues
        from datetime import date

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "cue_errors.jsonl"
            errors = [("autoevo_ran", "OSError: boom"), ("weekly", "ValueError: bad")]
            path = cues.record_cue_errors(errors, date(2099, 1, 1), log_path=log)
            self.assertEqual(path, log)
            lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["check"] for row in lines], ["autoevo_ran", "weekly"])
            cue = cues.cue_errors_cue(errors, path)
            self.assertEqual((cue.key, cue.severity), ("cue_errors", "hard"))
            self.assertIn("autoevo_ran, weekly", cue.message)
            self.assertIn(str(log), cue.message)


def _write_receipt(
    root: Path,
    routine: str,
    cycle: str,
    *,
    verification: str = "passed",
    recorded_routine: str | None = None,
    contract_version: int = 3,
) -> None:
    directory = root / "_meta" / "routine_receipts" / routine
    directory.mkdir(parents=True, exist_ok=True)
    output = root / "x" / f"{cycle}.md"
    output.parent.mkdir(exist_ok=True)
    output.write_text("fixture artifact\n", encoding="utf-8")
    binding = (
        f'artifact_sha256 = "{hashlib.sha256(output.read_bytes()).hexdigest()}"\n'
        'verification_scope = "artifact-bytes"\n'
    ) if contract_version == 4 else ""
    (directory / f"{cycle}.toml").write_text(
        f"contract_version = {contract_version}\n"
        f'routine = "{recorded_routine or routine}"\n'
        f'cycle_id = "{cycle}"\nverification = "{verification}"\n'
        f'output_file = "x/{cycle}.md"\n{binding}',
        encoding="utf-8",
    )


class LocalReceiptTests(unittest.TestCase):
    DECLARATION = {"name": "sample", "output_dir": "x", "file_pattern": "*.md"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atelier-cues-owner-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_latest_receipt_skips_a_mismatched_identity(self):
        _write_receipt(self.root, "sample", "2026-08-01")
        _write_receipt(self.root, "sample", "2026-08-05", recorded_routine="other")
        found = cues._latest_local_receipt(self.root, "sample", self.DECLARATION)
        self.assertIsNotNone(found)
        receipt_date, receipt, _ = found
        self.assertEqual(receipt_date.isoformat(), "2026-08-01")
        self.assertEqual(receipt["routine"], "sample")

    def test_latest_receipt_surfaces_an_unsupported_contract(self):
        _write_receipt(self.root, "sample", "2026-08-20", contract_version=2)
        found = cues._latest_local_receipt(self.root, "sample", self.DECLARATION)
        self.assertEqual(found[1]["verification"], "needs_review")

    def test_v3_remains_unbound_without_rewriting_evidence(self):
        _write_receipt(self.root, "sample", "2026-08-20")
        path = self.root / "_meta/routine_receipts/sample/2026-08-20.toml"
        before = path.read_bytes()
        (self.root / "x/2026-08-20.md").write_text("changed contents", encoding="utf-8")
        found = cues._latest_local_receipt(self.root, "sample", self.DECLARATION)
        self.assertEqual(found[1]["verification"], "passed")
        self.assertNotIn("artifact_sha256", found[1])
        self.assertNotIn("verification_scope", found[1])
        self.assertEqual(path.read_bytes(), before)
        with mock.patch.object(cues, "_routine_rows", return_value=([self.DECLARATION], None)):
            self.assertIn("v3: content unbound", cues._recap_local_runs(self.root, date(2026, 8, 20))[0])

    def test_invalid_latest_evidence_never_falls_back_to_an_older_success(self):
        _write_receipt(self.root, "sample", "2026-08-19", contract_version=4)
        for problem in ("changed", "empty", "missing", "hash", "scope", "version", "corrupt", "declaration"):
            with self.subTest(problem=problem):
                _write_receipt(self.root, "sample", "2026-08-20", contract_version=4)
                path = self.root / "_meta/routine_receipts/sample/2026-08-20.toml"
                output = self.root / "x/2026-08-20.md"
                declaration = self.DECLARATION
                valid = cues._latest_local_receipt(self.root, "sample", declaration)
                self.assertEqual(valid[1]["verification"], "passed")
                if problem == "changed":
                    original = output.read_bytes()
                    output.write_bytes(b"!" + original[1:])
                elif problem == "empty":
                    output.write_bytes(b"")
                elif problem == "missing":
                    output.unlink()
                elif problem == "declaration":
                    declaration = {**declaration, "file_pattern": "*.html"}
                else:
                    text = path.read_text()
                    text = {
                        "hash": text.replace("artifact_sha256", "ignored_hash"),
                        "scope": text.replace("artifact-bytes", "business-outcome"),
                        "version": text.replace("contract_version = 4", "contract_version = 99"),
                        "corrupt": "not valid toml",
                    }[problem]
                    path.write_text(text, encoding="utf-8")
                found = cues._latest_local_receipt(self.root, "sample", declaration)
                self.assertEqual(found[0], date(2026, 8, 20))
                self.assertEqual(found[1]["verification"], "needs_review")
                with mock.patch.object(cues, "_routine_rows", return_value=([declaration], None)):
                    self.assertEqual(cues._recap_local_runs(self.root, date(2026, 8, 20)), [])

    def test_autoevo_consumers_use_verified_json_and_do_not_hide_latest_failure(self):
        path = _autoevo_result(self.root, "2026-08-20")
        path.write_text(json.dumps({"schema_version": 1, "cycle_id": "2026-08-20", "status": "complete", "pending": ["p"]}))
        _write_receipt(self.root, "autoevo-nightly", "2026-08-21")
        with mock.patch.object(cues.autoevo_verify, "verify_cycle", return_value={"verified": True}) as verify:
            found = cues._latest_local_receipt(self.root, "autoevo-nightly")
            self.assertEqual(found[1]["verification"], "passed")
            self.assertEqual(found[2].suffix, ".json")
            verify.assert_called_once_with(vault=self.root, cycle="2026-08-20")
            self.assertIn("1 findings queued", cues._recap_local_runs(self.root, date(2026, 8, 20))[0])
            verify.side_effect = cues.autoevo_verify.VerificationError("commit proof failed")
            self.assertIsNone(cues._latest_local_receipt(self.root, "autoevo-nightly"))
            later = _autoevo_result(self.root, "2026-08-21")
            later.write_text(json.dumps({"schema_version": 1, "cycle_id": "2026-08-21", "status": "needs_review"}))
            verify.reset_mock()
            self.assertEqual(cues._latest_local_receipt(self.root, "autoevo-nightly")[1]["verification"], "needs_review")
            self.assertEqual(cues._recap_local_runs(self.root, date(2026, 8, 21)), [])
            verify.assert_not_called()
            later.write_text("broken")
            self.assertIsNone(cues._latest_local_receipt(self.root, "autoevo-nightly"))


class RoutineFailureCueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atelier-cues-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_only_the_latest_prefect_state_per_routine_counts(self):
        runs = [
            {
                "routine": "failed-routine",
                "state": "FAILED",
                "state_name": "Failed",
                "message": "codex exited",
            },
            {
                "routine": "failed-routine",
                "state": "COMPLETED",
                "state_name": "Completed",
                "message": "",
            },
            {
                "routine": "healthy-routine",
                "state": "COMPLETED",
                "state_name": "Completed",
                "message": "",
            },
        ]
        with mock.patch.object(cues.routine_status, "recent_runs", return_value=runs):
            cue, debug = cues.check_routine_failures(self.root, date(2026, 8, 31))
        self.assertIsNotNone(cue)
        self.assertIn("failed-routine", cue.message)
        self.assertIn("codex exited", cue.message)
        self.assertIn("failed=1", debug)

    def test_a_newer_success_supersedes_an_older_failure(self):
        runs = [
            {"routine": "sample", "state": "COMPLETED", "state_name": "Completed", "message": ""},
            {"routine": "sample", "state": "FAILED", "state_name": "Failed", "message": "old"},
        ]
        with mock.patch.object(cues.routine_status, "recent_runs", return_value=runs):
            cue, debug = cues.check_routine_failures(self.root, date(2026, 8, 31))
        self.assertIsNone(cue)
        self.assertIn("failed=0", debug)

    def test_process_failures_are_included(self):
        failure = {"routine": "process-fixture", "state": "FAILED", "state_name": "Failed", "message": "exit 1"}

        def recent_runs(since, *, model_only):
            return [] if model_only else [failure]

        with mock.patch.object(cues.routine_status, "recent_runs", side_effect=recent_runs):
            cue, debug = cues.check_routine_failures(self.root, date(2026, 8, 31))
        self.assertIn("process-fixture", cue.message)
        self.assertIn("failed=1", debug)

    def test_latest_deferred_state_supersedes_failure_without_counting_as_failure(self):
        runs = [
            {"routine": "sample", "state": "CANCELLED", "state_name": "Deferred", "message": "session active"},
            {"routine": "sample", "state": "FAILED", "state_name": "Failed", "message": "older failure"},
            {"routine": "cancelled", "state": "CANCELLED", "state_name": "Cancelled", "message": "stopped"},
        ]
        with mock.patch.object(cues.routine_status, "recent_runs", return_value=runs):
            cue, debug = cues.check_routine_failures(self.root, date(2026, 8, 31))
        self.assertNotIn("sample", cue.message)
        self.assertIn("cancelled", cue.message)
        self.assertIn("failed=1", debug)

    def test_api_unavailability_surfaces_only_after_installation_evidence(self):
        unavailable = cues.routine_status.StatusUnavailable("offline")
        with mock.patch.object(cues.routine_status, "recent_runs", side_effect=unavailable), mock.patch.dict(
            os.environ, {}, clear=True
        ):
            cue, debug = cues.check_routine_failures(self.root, date(2026, 8, 31))
            self.assertIsNone(cue)
            self.assertIn("no installation evidence", debug)

            (self.root / "_meta/routine_receipts").mkdir(parents=True)
            cue, debug = cues.check_routine_failures(self.root, date(2026, 8, 31))
            self.assertIsNotNone(cue)
            self.assertIn("status is unavailable", cue.message)
            self.assertIn("offline", debug)


class LocalRoutineMissedTests(unittest.TestCase):
    WATCH = textwrap.dedent("""
        version = 1

        [[routine]]
        name = "r"
        label = "demo routine"
        execution = "local"
        runner = "model"
        output_dir = "x"
        cron = "0 6 * * *"
    """).strip()

    def _vault(self, tmp: str) -> Path:
        vault = Path(tmp)
        meta = vault / "_meta"
        meta.mkdir(parents=True)
        registry_dir = vault / "_tools/routines"
        registry_dir.mkdir(parents=True)
        (registry_dir / "registry.toml").write_text(self.WATCH, encoding="utf-8")
        (meta / "routine_receipts" / "r").mkdir(parents=True)
        (vault / "x").mkdir()
        return vault

    def _run(self, vault: Path):
        return cues.check_local_routine_missed(
            vault,
            date(2026, 8, 31),
            now=datetime(2026, 8, 31, 22, 0).astimezone(),
        )

    def test_missing_due_receipt_fires(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            cue, debug = self._run(self._vault(tmp))
            self.assertIsNotNone(cue)
            self.assertIn("demo routine", cue.message)
            self.assertIn("receipt absent", debug)

    def test_verified_due_receipt_is_silent(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp)
            _write_receipt(vault, "r", "2026-08-31")
            cue, debug = self._run(vault)
            self.assertIsNone(cue)
            self.assertIn("verified", debug)

    def test_blocked_due_receipt_names_the_blocker(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp)
            receipt = vault / "_meta/routine_receipts/r/2026-08-31.toml"
            receipt.write_text(
                'contract_version = 3\nroutine = "r"\ncycle_id = "2026-08-31"\n'
                'verification = "blocked"\nblocker = "session_active"\n',
                encoding="utf-8",
            )
            cue, debug = self._run(vault)
            self.assertIsNotNone(cue)
            self.assertIn("session_active", cue.message)
            self.assertIn("blocked", debug)

    def test_cron_array_uses_one_cycle_date(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp)
            watch = vault / "_tools/routines/registry.toml"
            watch.write_text(
                watch.read_text(encoding="utf-8").replace(
                    'cron = "0 6 * * *"', 'cron = ["0 6 * * *", "0 18 * * *"]'
                ),
                encoding="utf-8",
            )
            _write_receipt(vault, "r", "2026-08-31")
            cue, debug = self._run(vault)
            self.assertIsNone(cue)
            self.assertIn("verified", debug)

    def test_explicit_timezone_reaches_receipt_and_hitrate_checks(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp)
            watch = vault / "_tools/routines/registry.toml"
            watch.write_text(self.WATCH.replace(
                'cron = "0 6 * * *"', 'cron = ["0 0 * * *", "0 12 * * *"]\ntimezone = "UTC"'
            ), encoding="utf-8")
            now = datetime(2026, 8, 31, 17, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
            _write_receipt(vault, "r", "2026-08-31")
            cue, _ = cues.check_local_routine_missed(vault, now.date(), now=now)
            self.assertIn("2026-09-01", cue.message)
            _write_receipt(vault, "r", "2026-09-01")
            self.assertIsNone(cues.check_local_routine_missed(vault, now.date(), now=now)[0])
            # The receipt fixtures already create artifacts for 08-31 and 09-01.
            # Add one earlier delivery so this remains a deliberate 3/4 window.
            for day in ("2026-08-29",):
                (vault / "x" / (day + ".md")).write_text("delivered", encoding="utf-8")
            cue, debug = cues.check_routine_hitrate(vault, now.date(), now=now)
            self.assertIsNone(cue, debug)
            self.assertIn("3/4", debug)

    def test_hitrate_reports_output_dates_without_claiming_execution(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp)
            for filename in ("2026-08-20.md", "2026-08-20-extra.md", "2026-08-31.md"):
                (vault / "x" / filename).write_text("output", encoding="utf-8")
            now = datetime(2026, 8, 31, 22, 0).astimezone()
            cue, debug = cues.check_routine_hitrate(vault, now.date(), now=now)
            self.assertEqual(cue.key, "routine_hitrate")
            self.assertIn("output-date coverage", cue.message)
            self.assertIn("2/12 expected dates with output", cue.message)
            self.assertNotIn("runs", cue.message)
            self.assertNotIn("scheduler is firing", cue.message)
            self.assertIn("degraded=1", debug)

    def test_conditional_autoevo_notes_do_not_trigger_artifact_cadence_alerts(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp)
            watch = vault / "_tools/routines/registry.toml"
            watch.write_text(self.WATCH + '\nadapter = "autoevo"\n', encoding="utf-8")
            now = datetime(2026, 8, 31, 22, 0).astimezone()
            (vault / "x/2026-08-01.md").write_text("old actionable report", encoding="utf-8")
            self.assertIsNone(cues.check_routine_staleness(vault, now.date())[0])
            self.assertIsNone(cues.check_routine_hitrate(vault, now.date(), now=now)[0])
            cue, _ = cues.check_local_routine_missed(vault, now.date(), now=now)
            self.assertIsNotNone(cue)


class VaultLayoutCueTest(unittest.TestCase):
    def test_quiet_on_linked_vault_and_fires_on_stale_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, store = Path(tmp) / "vault", Path(tmp) / "store"
            (vault / ".git").mkdir(parents=True)
            (vault / "a").mkdir()
            (store / "a" / "raw").mkdir(parents=True)
            (vault / "a" / "raw").symlink_to(store / "a" / "raw")
            with mock.patch.dict(os.environ, {"OV": str(vault)}), \
                    mock.patch.object(cues, "raw_store", return_value=store):
                self.assertIsNone(cues.check_vault_layout(vault, date(2099, 1, 5))[0])
                cue, _ = cues.check_vault_layout(Path(tmp) / "zk", date(2099, 1, 5))
        self.assertEqual((cue.key, cue.severity), ("vault_layout", "hard"))
        self.assertIn("1 个问题", cue.message)
        self.assertIn("missing", cue.message)


if __name__ == "__main__":
    unittest.main()
