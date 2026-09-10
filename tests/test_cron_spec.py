"""Outcome tests for Prefect-backed routine cron evaluation."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cron_spec as cs  # noqa: E402


class ScheduledDatesTests(unittest.TestCase):
    def test_ranges_names_sunday_and_month_steps(self):
        cases = (
            ("0 9 * * mon-fri UTC", date(2026, 8, 24), date(2026, 8, 30), 5),
            ("0 10 * * 7 UTC", date(2026, 8, 24), date(2026, 8, 30), 1),
            ("0 9 1 */3 * UTC", date(2026, 1, 1), date(2026, 12, 31), 4),
            ("0 9 1 mar * UTC", date(2026, 1, 1), date(2026, 12, 31), 1),
        )
        for cron, start, end, count in cases:
            with self.subTest(cron=cron):
                now = datetime.combine(end, datetime.max.time(), timezone.utc)
                self.assertEqual(len(cs.scheduled_dates(cron, start, now)), count)

    def test_prefect_day_of_month_and_week_use_or(self):
        now = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
        self.assertEqual(
            cs.scheduled_dates("0 9 1 * 1 UTC", date(2026, 8, 31), now),
            [date(2026, 8, 31), date(2026, 9, 1)],
        )

    def test_occurrence_at_now_is_included(self):
        start = date(2026, 8, 31)
        just_before = datetime(2026, 8, 31, 8, 59, tzinfo=timezone.utc)
        on_time = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(cs.scheduled_dates("0 9 * * * UTC", start, just_before), [])
        self.assertEqual(cs.scheduled_dates("0 9 * * * UTC", start, on_time), [start])

    def test_explicit_zone_defines_cycle_date(self):
        start = date(2026, 8, 31)
        now = datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc)
        for zone, expected in (
            ("UTC", [start, date(2026, 9, 1)]),
            ("America/Los_Angeles", [start]),
        ):
            with self.subTest(zone=zone):
                self.assertEqual(cs.scheduled_dates("0 0 * * *", start, now, zone), expected)
        with mock.patch("tzlocal.get_localzone", return_value=ZoneInfo("America/Los_Angeles")):
            self.assertEqual(cs.scheduled_dates("0 0 * * *", start, now, "local"), [start])

    def test_explicit_zone_observes_spring_dst_cutoff(self):
        start = date(2026, 3, 5)
        before = datetime.fromisoformat("2026-03-08T15:30:00+00:00")
        after = datetime.fromisoformat("2026-03-08T16:30:00+00:00")
        expected = [date(2026, 3, day) for day in (5, 6, 7)]
        self.assertEqual(cs.scheduled_dates("0 9 * * *", start, before, "America/Los_Angeles"), expected)
        self.assertEqual(
            cs.scheduled_dates("0 9 * * *", start, after, "America/Los_Angeles"),
            [*expected, date(2026, 3, 8)],
        )

    def test_explicit_zone_observes_fall_dst_cutoff(self):
        start = date(2026, 10, 29)
        before = datetime.fromisoformat("2026-11-01T16:30:00+00:00")
        after = datetime.fromisoformat("2026-11-01T17:30:00+00:00")
        expected = [date(2026, 10, day) for day in (29, 30, 31)]
        self.assertEqual(cs.scheduled_dates("0 9 * * *", start, before, "America/Los_Angeles"), expected)
        self.assertEqual(
            cs.scheduled_dates("0 9 * * *", start, after, "America/Los_Angeles"),
            [*expected, date(2026, 11, 1)],
        )

    def test_invalid_declarations_have_no_dates_or_cadence(self):
        now = datetime(2026, 8, 31, tzinfo=timezone.utc)
        for cron in (
            "0 9 32 * *",
            "0 9 * 13 *",
            "0 9 * * 8",
            "0 9 */0 * *",
            "0 9 r(1-7) * *",
            "0 9 r/2 * *",
            "0 9 h(1-7) * *",
        ):
            with self.subTest(cron=cron):
                self.assertEqual(cs.scheduled_dates(cron, date(2026, 8, 1), now), [])
                self.assertIsNone(cs.estimate_cadence_days(cron))


class CadenceTests(unittest.TestCase):
    def test_cadence_comes_from_occurrence_spacing(self):
        cases = (
            ("0 9 * * *", 1),
            ("0 9 * * 1-5", 1),
            ("0 9 * * 3", 7),
            ("0 12 */3 * *", 3),
            ("0 9 1 * *", 30),
            ("0 9 15 2,5,8,11 *", 91),
        )
        for cron, expected in cases:
            with self.subTest(cron=cron):
                self.assertEqual(cs.estimate_cadence_days(cron), expected)
