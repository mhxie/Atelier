"""Tests for cron-aware cycle selection in scripts/routine_claim.py.

Why this exists: before it, `routine_runner.sh` took `date +%Y-%m-%d` as the
cycle for every routine except autoevo-nightly, and nothing in the execution
path read the declared cron. The plist *was* the schedule, so giving a weekly
routine an hourly plist -- the obvious fix for a routine that loses a whole
cycle to one deferral -- would have silently turned it into a daily routine.

These pin the property that makes an hourly plist safe: the cron decides the
cadence, the plist only decides how often we check. Selection picks *which*
cycle; `schedule_decision` still owns whether to act on it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import routine_claim as rc  # noqa: E402

# Monday 06:06 local. Every assertion below is anchored on a real weekday.
WEEKLY_CRON = "6 6 * * 1"
DAILY_CRON = "17 6 * * *"
MONTHLY_CRON = "0 9 15 * *"



def local(year: int, month: int, day: int, hour: int) -> datetime:
    """A wall-clock local time.

    These crons carry no ` UTC` marker, so they are declared in local time and
    the test must be too. Building from UTC here would make every assertion
    depend on the machine's offset.
    """
    return datetime(year, month, day, hour, 0).astimezone()


MON = local(2026, 8, 24, 7)  # after the 06:06 occurrence
TUE = local(2026, 8, 25, 7)
SUN = local(2026, 8, 30, 7)  # 6 days after Monday


def _watch(cron: str | None) -> str:
    row = 'name = "r"\noutput_dir = "x"\n'
    if cron is not None:
        row += f'cron = "{cron}"\n'
    return f"[[routine]]\n{row}"


class CronSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "_meta").mkdir()
        self._prev = os.environ.get("OV")
        os.environ["OV"] = str(self.vault)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("OV", None)
        else:
            os.environ["OV"] = self._prev
        self.tmp.cleanup()

    def _declare(self, cron: str | None) -> None:
        (self.vault / "_meta" / "routine_watch.toml").write_text(
            _watch(cron), encoding="utf-8"
        )

    def _claim(self, cycle: str, status: str) -> None:
        directory = self.vault / "_meta" / "routine_runs" / "r"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{cycle}.toml").write_text(
            textwrap.dedent(f"""
                routine = "r"
                cycle_id = "{cycle}"
                status = "{status}"
            """).strip()
            + "\n",
            encoding="utf-8",
        )

    def select(self, when: datetime) -> dict:
        return rc.select_scheduled_cycle("r", now=when)

    # --- the property that makes an hourly plist safe --------------------

    def test_weekly_routine_runs_on_its_scheduled_day(self):
        self._declare(WEEKLY_CRON)
        result = self.select(MON)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["cycle_id"], "2026-08-24")

    def test_weekly_routine_does_not_run_again_the_next_day(self):
        """The whole point: an hourly plist must not make this daily."""
        self._declare(WEEKLY_CRON)
        self._claim("2026-08-24", "completed")
        result = self.select(TUE)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "latest-scheduled-cycle-completed")

    def test_a_missed_weekly_cycle_is_caught_up_later_in_the_week(self):
        self._declare(WEEKLY_CRON)
        result = self.select(TUE)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["reason"], "missed-scheduled-cycle")
        self.assertEqual(result["cycle_id"], "2026-08-24")

    def test_catch_up_still_works_six_days_late(self):
        self._declare(WEEKLY_CRON)
        result = self.select(SUN)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["cycle_id"], "2026-08-24")

    def test_an_unresolved_cycle_is_returned_for_schedule_decision_to_refuse(self):
        """Selection does not own claim policy; it hands the cycle over."""
        self._declare(WEEKLY_CRON)
        self._claim("2026-08-24", "failed")
        result = self.select(TUE)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["reason"], "scheduled-cycle-unresolved")
        # And the gate that owns policy still refuses it.
        decision = rc.schedule_decision("r", "2026-08-24")
        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "claim-failed")

    def test_before_the_scheduled_hour_today_is_not_yet_due(self):
        self._declare(WEEKLY_CRON)
        self._claim("2026-08-17", "completed")
        early = local(2026, 8, 24, 5)  # before the 06:06 occurrence
        result = self.select(early)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["cycle_id"], "2026-08-17")

    # --- daily and monthly ----------------------------------------------

    def test_daily_routine_runs_each_day(self):
        self._declare(DAILY_CRON)
        self._claim("2026-08-24", "completed")
        result = self.select(TUE)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["cycle_id"], "2026-08-25")

    def test_a_monthly_cycle_is_not_caught_up_indefinitely(self):
        """MAX_CATCHUP_DAYS stops a monthly routine running 20 days late."""
        self._declare(MONTHLY_CRON)
        far_past = local(2026, 8, 30, 12)
        result = self.select(far_past)
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "no-scheduled-occurrence-due")

    def test_a_monthly_cycle_is_caught_up_inside_the_window(self):
        self._declare(MONTHLY_CRON)
        soon_after = local(2026, 8, 18, 12)
        result = self.select(soon_after)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["cycle_id"], "2026-08-15")

    # --- failing open ----------------------------------------------------

    def test_a_routine_without_a_declared_cron_keeps_the_old_behaviour(self):
        self._declare(None)
        result = self.select(TUE)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["reason"], "current-cycle-no-declared-cron")
        self.assertEqual(result["cycle_id"], "2026-08-25")

    def test_an_unevaluable_cron_runs_rather_than_silently_stopping(self):
        """A typo in a schedule must not disable a routine."""
        self._declare("not a cron")
        result = self.select(TUE)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["reason"], "current-cycle-unevaluable-cron")

    def test_a_wildcard_hour_is_unevaluable_rather_than_never_due(self):
        self._declare("0 * * * 1")
        result = self.select(TUE)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["reason"], "current-cycle-unevaluable-cron")

    def test_a_missing_watch_file_fails_open(self):
        result = self.select(TUE)
        self.assertEqual(result["action"], "run")
        self.assertEqual(result["reason"], "current-cycle-no-declared-cron")

    # --- the one routine that keeps its bespoke rule ---------------------

    def test_autoevo_still_uses_its_own_pre_dawn_rule(self):
        self._declare(WEEKLY_CRON)
        result = rc.select_scheduled_cycle(
            "autoevo-nightly", now=local(2026, 8, 25, 12)
        )
        self.assertEqual(result["reason"], "primary-or-missed-current-cycle")


if __name__ == "__main__":
    unittest.main()
