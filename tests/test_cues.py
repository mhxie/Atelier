"""Regression tests for the autoevo cue in scripts/cues.py.

Glitches (2026-08-22): the nightly bot was blocked 73 of 103 attempts by the
same gate while the cue stayed soft; and when the runner crashed before the
audit step the cue said "did not run" with a generic cause list although the
claim file held the real error. Both lookups must also work once
`agent-findings/` is bucketed.
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
        env={**os.environ, "OV": str(vault), "ATELIER_SKIP_LOCK_TOUCH": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _audit(vault: Path, day: str, gate: str | None, bucket: bool) -> None:
    folder = vault / "agent-findings" / (day[:7] if bucket else "")
    folder.mkdir(parents=True, exist_ok=True)
    skipped = f"- {gate}: detail\n" if gate else "- (none)\n"
    (folder / f"autoevo-applied-{day}.md").write_text(
        f"## Autoevo Run: {day} 05:00\n\n### Skipped (reason)\n{skipped}\n### Errors\n- (none)\n",
        encoding="utf-8",
    )


class AutoevoCueTest(unittest.TestCase):
    def test_same_gate_three_days_escalates_to_hard_on_bucketed_tier(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            for day in ("2099-01-03", "2099-01-04", "2099-01-05"):
                _audit(vault, day, "dirty_autoevo_state", bucket=True)
            out = _run_py(
                vault,
                """
                cue, debug = cues.check_autoevo_ran(vault, date(2099, 1, 5), now=datetime(2099, 1, 5, 12, 0))
                print(json.dumps({"severity": cue.severity if cue else None, "message": cue.message if cue else "", "debug": debug}))
                """,
            )
            self.assertEqual(out["severity"], "hard", out)
            self.assertIn("3 consecutive days", out["message"])
            self.assertIn("--dirty-scope", out["message"])

    def test_specific_fix_text_for_each_gate(self) -> None:
        """privacy/semantic gates must get their specific fix, not the generic one."""
        for gate, expect in (
            ("privacy_hits", "privacy_check.py"),
            ("semantic_unavailable", "semantic index"),
        ):
            with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
                vault = Path(tmp)
                for day in ("2099-01-03", "2099-01-04", "2099-01-05"):
                    _audit(vault, day, gate, bucket=False)
                out = _run_py(
                    vault,
                    """
                    cue, debug = cues.check_autoevo_ran(vault, date(2099, 1, 5), now=datetime(2099, 1, 5, 12, 0))
                    print(json.dumps({"severity": cue.severity if cue else None, "message": cue.message if cue else ""}))
                    """,
                )
                self.assertEqual(out["severity"], "hard", (gate, out))
                self.assertIn(expect, out["message"], (gate, out))
                self.assertNotIn("see the audit file", out["message"], (gate, out))

    def test_gate_fix_keys_match_preflight_gate_strings(self) -> None:
        """Producer/consumer pin: every gate_fixes key must exist in autoevo_preflight.py."""
        import re
        cues_src = (REPO_ROOT / "scripts" / "cues.py").read_text(encoding="utf-8")
        block = cues_src.split("gate_fixes = {", 1)[1].split("}", 1)[0]
        keys = re.findall(r'^\s*"([a-z_]+)":', block, re.MULTILINE)
        self.assertGreaterEqual(len(keys), 8, keys)
        preflight_src = (REPO_ROOT / "scripts" / "autoevo_preflight.py").read_text(encoding="utf-8")
        for key in keys:
            self.assertIn(f'"{key}"', preflight_src, f"gate_fixes key {key!r} not emitted by autoevo_preflight.py")

    def test_every_emitted_gate_has_a_fix_entry(self) -> None:
        """Reverse pin: a NEW preflight gate must not fall back to generic text."""
        import re
        preflight_src = (REPO_ROOT / "scripts" / "autoevo_preflight.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'"gate": "([a-z_]+)"', preflight_src))
        # dynamic result["gate"] assignments too
        emitted |= set(re.findall(r'result\["gate"\] = "([a-z_]+)"', preflight_src))
        self.assertGreaterEqual(len(emitted), 9, emitted)
        cues_src = (REPO_ROOT / "scripts" / "cues.py").read_text(encoding="utf-8")
        block = cues_src.split("gate_fixes = {", 1)[1].split("}", 1)[0]
        keys = set(re.findall(r'"([a-z_]+)":', block))
        missing = emitted - keys - {"unknown"}
        self.assertFalse(missing, f"preflight gates without a fix entry: {sorted(missing)}")

    def test_two_days_stays_soft(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            _audit(vault, "2099-01-04", "dirty_autoevo_state", bucket=False)
            _audit(vault, "2099-01-05", "dirty_autoevo_state", bucket=False)
            out = _run_py(
                vault,
                """
                cue, debug = cues.check_autoevo_ran(vault, date(2099, 1, 5), now=datetime(2099, 1, 5, 12, 0))
                print(json.dumps({"severity": cue.severity if cue else None}))
                """,
            )
            self.assertEqual(out["severity"], "soft")

    def test_missing_audit_surfaces_claim_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            _audit(vault, "2099-01-04", None, bucket=True)  # bot installed; yesterday clean
            claim = vault / "_meta" / "routine_runs" / "autoevo-nightly" / "2099-01-05.toml"
            claim.parent.mkdir(parents=True)
            claim.write_text('status = "failed"\nerror = "runner-exited-unexpectedly"\n', encoding="utf-8")
            out = _run_py(
                vault,
                """
                cue, debug = cues.check_autoevo_ran(vault, date(2099, 1, 5), now=datetime(2099, 1, 5, 12, 0))
                print(json.dumps({"message": cue.message if cue else ""}))
                """,
            )
            self.assertIn("runner-exited-unexpectedly", out["message"])
            self.assertIn("failed", out["message"])


class IntentMissCueTest(unittest.TestCase):
    """The cue fires on the review's own signal (a phrase unrouted on 3+
    distinct days), not on a raw miss count that never proposed anything."""

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


class RoutineCueTest(unittest.TestCase):
    def test_oldest_unreviewed_output_gets_a_visible_slot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = Path(tmp)
            meta = vault / "_meta"
            meta.mkdir()
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
            (meta / "routine_watch.toml").write_text("\n".join(rows))

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


if __name__ == "__main__":
    unittest.main()


sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cues  # noqa: E402


def _write_claim(root, routine, cycle, machine, status="completed"):
    d = root / "_meta" / "routine_runs" / routine
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cycle}.toml").write_text(
        f'routine = "{routine}"\ncycle_id = "{cycle}"\n'
        f'machine = "{machine}"\nstatus = "{status}"\n',
        encoding="utf-8",
    )


def _owner(root, label):
    meta = root / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "routine_owner.toml").write_text(
        f'version = 1\nowner_id = "abc"\nowner_label = "{label}"\n'
        'transferred_at = "2026-07-17T08:09:18+00:00"\n',
        encoding="utf-8",
    )
    return {"coordination": {"backend": "owner"}}


def test_machine_filter_ignores_foreign_records(tmp_path):
    config = _owner(tmp_path, "Devbox")
    _write_claim(tmp_path, "sample", "2026-08-01", "Devbox", status="failed")
    _write_claim(tmp_path, "sample", "2026-08-05", "Foreign-Host.local", status="failed")

    label = cues._local_owner_label(tmp_path, config)
    assert label == "Devbox"
    found = cues._latest_local_claim(tmp_path, "sample", machine=label)
    assert found is not None
    claim_date, claim, _ = found
    assert claim_date.isoformat() == "2026-08-01"
    assert claim["machine"] == "Devbox"


def test_diverged_owner_label_fails_open_instead_of_wiping_the_fleet(tmp_path):
    # owner_label froze at claim time; the writer now records the live hostname
    config = _owner(tmp_path, "Devbox")
    _write_claim(tmp_path, "sample", "2026-08-20", "Devbox.local")

    # the stale label would drop every record and report the fleet as missed
    assert cues._latest_local_claim(tmp_path, "sample", machine="Devbox") is None
    # so the helper must decline to hand that label to the filter
    label = cues._local_owner_label(tmp_path, config)
    assert label is None
    found = cues._latest_local_claim(tmp_path, "sample", machine=label)
    assert found is not None and found[1]["machine"] == "Devbox.local"


def test_corrupt_machine_value_cannot_win_latest_claim(tmp_path):
    config = _owner(tmp_path, "Devbox")
    _write_claim(tmp_path, "sample", "2026-08-01", "Devbox")
    d = tmp_path / "_meta" / "routine_runs" / "sample"
    (d / "2026-08-09.toml").write_text(
        'routine = "sample"\nmachine = ["Devbox"]\nstatus = "completed"\n',
        encoding="utf-8",
    )

    label = cues._local_owner_label(tmp_path, config)
    found = cues._latest_local_claim(tmp_path, "sample", machine=label)
    assert found is not None
    assert found[0].isoformat() == "2026-08-01"


class RoutineFailureCueTests(unittest.TestCase):
    """Pre-claim failures were written to disk and read by nothing.

    Every other routine cue reads claim files, but the runner writes a
    diagnostic precisely when it dies *before* a claim exists, so a routine
    could fail this way indefinitely while looking merely stale.
    """

    def _vault(self, tmp: str, routine: str, recorded_at: str, phase: str) -> Path:
        vault = Path(tmp)
        directory = vault / "_meta" / "routine_failures" / routine
        directory.mkdir(parents=True)
        (directory / "20260831T000000-host-1.toml").write_text(
            textwrap.dedent(f"""
                routine = "{routine}"
                cycle_id = "2026-08-31"
                machine = "host"
                recorded_at = "{recorded_at}"
                phase = "{phase}"
                status = "failed"
                error = "boom"
            """).strip(),
            encoding="utf-8",
        )
        return vault

    def test_a_recent_diagnostic_fires(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import cues
        from datetime import date

        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp, "some-routine", "2026-08-30T02:00:00-07:00", "lock-acquire")
            cue, _debug = cues.check_routine_failures(vault, date(2026, 8, 31))
            self.assertIsNotNone(cue)
            self.assertIn("some-routine", cue.message)
            self.assertIn("lock-acquire", cue.message)

    def test_an_old_diagnostic_is_history_not_a_cue(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import cues
        from datetime import date

        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp, "some-routine", "2026-07-01T02:00:00-07:00", "lock-acquire")
            cue, debug = cues.check_routine_failures(vault, date(2026, 8, 31))
            self.assertIsNone(cue)
            self.assertIn("older than 7d", debug)

    def test_a_missing_directory_is_silent(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import cues
        from datetime import date

        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            cue, _debug = cues.check_routine_failures(Path(tmp), date(2026, 8, 31))
            self.assertIsNone(cue)


class ClaimFailureReasonTests(unittest.TestCase):
    """The cue has to carry the reason, because the transcript will not.

    Every routine plist sends the runner's output to /tmp, which macOS purges,
    so by the time a human reads "failed" the evidence is usually gone. The
    claim's own fields are what survive.
    """

    def setUp(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import cues

        self.reason = cues._claim_failure_reason

    def test_phase_and_detail_are_combined(self):
        self.assertEqual(
            self.reason(
                {"error": "model-execution-failed", "error_detail": "codex stream died"}
            ),
            "model-execution-failed: codex stream died",
        )

    def test_phase_alone_survives(self):
        self.assertEqual(
            self.reason({"error": "model-execution-failed"}), "model-execution-failed"
        )

    def test_detail_alone_survives(self):
        self.assertEqual(self.reason({"error_detail": "boom"}), "boom")

    def test_a_claim_with_neither_says_nothing_rather_than_guessing(self):
        self.assertEqual(self.reason({}), "")

    def test_a_detail_that_already_repeats_the_phase_is_not_doubled(self):
        self.assertEqual(
            self.reason(
                {"error": "lock-acquire-failed", "error_detail": "lock-acquire-failed: no profile"}
            ),
            "lock-acquire-failed: no profile",
        )


class OwnershipTransferGraceTests(unittest.TestCase):
    """A transfer must not make every routine look missed.

    Claims are filtered to the owning machine, so the day ownership moves, every
    cycle the previous owner completed reads as a cycle this machine never ran.
    Observed on 2026-08-31: a transfer at 21:11 local produced five false
    "no claim" reports for work finished fifteen hours earlier.
    """

    WATCH = textwrap.dedent("""
        [coordination]
        backend = "owner"

        [[routine]]
        name = "r"
        label = "demo routine"
        execution = "local"
        output_dir = "x"
        cron = "0 6 * * *"
    """).strip()

    def _vault(self, tmp: str, transferred: str) -> Path:
        vault = Path(tmp)
        meta = vault / "_meta"
        meta.mkdir(parents=True)
        (meta / "routine_watch.toml").write_text(self.WATCH, encoding="utf-8")
        (meta / "routine_owner.toml").write_text(
            textwrap.dedent(f"""
                version = 2
                owner_id = "00000000-0000-0000-0000-000000000000"
                owner_label = "ThisMachine"
                generation = 2
                transferred_at = "{transferred}"
            """).strip(),
            encoding="utf-8",
        )
        (meta / "routine_runs" / "r").mkdir(parents=True)
        (vault / "x").mkdir()
        return vault

    def _run(self, vault: Path):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import cues
        from datetime import date, datetime

        return cues.check_local_routine_missed(
            vault,
            date(2026, 8, 31),
            now=datetime(2026, 8, 31, 22, 0).astimezone(),
        )

    def test_a_transfer_today_silences_the_cue(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp, "2026-08-31T21:11:57+00:00")
            cue, _debug = self._run(vault)
            self.assertIsNone(cue)

    def test_an_old_transfer_still_reports_a_real_miss(self):
        with tempfile.TemporaryDirectory(prefix="atelier-cues-") as tmp:
            vault = self._vault(tmp, "2026-06-01T00:00:00+00:00")
            cue, _debug = self._run(vault)
            self.assertIsNotNone(cue)
            self.assertIn("demo routine", cue.message)
