"""Tests for scripts/cron_spec.py field parsing.

Why this exists: routine_claim gates cycle selection on `is_evaluable` and
`scheduled_dates`. Before these, a range (`1-5`), Sunday as `7`, or a month
step all parsed as "evaluable" and then never matched, so the routine was
skipped on every fire with `no-scheduled-occurrence-due` and nothing said why.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cron_spec as cs  # noqa: E402

MON = date(2026, 8, 24)
SUN = date(2026, 8, 30)


class FieldValuesTests(unittest.TestCase):
    def test_star_and_literals(self):
        self.assertEqual(cs.field_values("*", 1, 3), {1, 2, 3})
        self.assertEqual(cs.field_values("2,5", 1, 12), {2, 5})

    def test_ranges_and_steps(self):
        self.assertEqual(cs.field_values("1-5", 0, 7), {1, 2, 3, 4, 5})
        self.assertEqual(cs.field_values("*/3", 1, 12), {1, 4, 7, 10})
        self.assertEqual(cs.field_values("*/3", 0, 7), {0, 3, 6})
        self.assertEqual(cs.field_values("1-10/4", 1, 31), {1, 5, 9})
        self.assertEqual(cs.field_values("5/10", 1, 31), {5, 15, 25})

    def test_invalid_fields_are_none_not_empty(self):
        for field in ("x", "1-", "*/0", "0", "13", "5-2", "1-32", "a-b", "*/x"):
            self.assertIsNone(cs.field_values(field, 1, 12 if field != "1-32" else 31), field)


class MatchingTests(unittest.TestCase):
    def test_weekday_range_matches_monday(self):
        self.assertTrue(cs.matches_date("0 9 * * 1-5", MON))
        self.assertFalse(cs.matches_date("0 9 * * 1-5", SUN))

    def test_seven_is_sunday(self):
        self.assertTrue(cs.matches_date("0 10 * * 7", SUN))
        self.assertTrue(cs.matches_date("0 10 * * 0", SUN))
        self.assertFalse(cs.matches_date("0 10 * * 7", MON))

    def test_month_step_counts_from_january(self):
        months = [m for m in range(1, 13) if cs.matches_date("0 9 1 */3 *", date(2026, m, 1))]
        self.assertEqual(months, [1, 4, 7, 10])

    def test_scheduled_dates_honours_ranges(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        due = cs.scheduled_dates("0 9 * * 1-5 UTC", date(2026, 8, 24), now)
        self.assertEqual([d.isoformat() for d in due], [
            "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
        ])


class EvaluabilityTests(unittest.TestCase):
    def test_valid_day_fields_are_evaluable(self):
        for cron in ("0 9 * * 1-5", "0 10 * * 7", "0 9 1 */3 *", "0 13 * * 1 UTC (Mon)"):
            self.assertTrue(cs.is_evaluable(cron), cron)

    def test_unparseable_day_fields_fail_open(self):
        """A typo must read as unevaluable, which routine_claim runs, not as never due."""
        for cron in ("0 9 * * mon", "0 9 32 * *", "0 9 * 13 *", "0 9 * * 8", "0 9 */0 * *"):
            self.assertFalse(cs.is_evaluable(cron), cron)
            self.assertEqual(cs.scheduled_dates(cron, date(2026, 8, 1), datetime.now(timezone.utc)), [])

    def test_cadence_for_month_step(self):
        self.assertEqual(cs.estimate_cadence_days("0 9 1 */3 *"), 90)
        self.assertEqual(cs.estimate_cadence_days("0 9 * * 1-5"), 7)
