"""daily_context.py: quota from local snapshots, weather from a fetcher."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import daily_context as dc  # noqa: E402

NOW = 1_788_400_000.0  # arbitrary fixed clock


class QuotaColourTests(unittest.TestCase):
    def test_level_thresholds_are_on_the_remaining_share(self):
        self.assertEqual(dc.quota_level(83), "ok")
        self.assertEqual(dc.quota_level(41), "ok")
        self.assertEqual(dc.quota_level(40), "low")
        self.assertEqual(dc.quota_level(21), "low")
        self.assertEqual(dc.quota_level(20), "critical")
        self.assertEqual(dc.quota_level(0), "critical")

    def test_relative_reset_counts_down_in_days_and_hours(self):
        self.assertEqual(dc.relative_reset(NOW + 2 * 86400 + 3 * 3600 + 59, NOW), "2 天 3 小时后重置")
        self.assertEqual(dc.relative_reset(NOW + 5 * 3600, NOW), "5 小时后重置")
        self.assertEqual(dc.relative_reset(NOW + 20 * 60, NOW), "20 分钟后重置")
        self.assertEqual(dc.relative_reset(NOW - 10, NOW), "0 分钟后重置")


class SnapshotTests(unittest.TestCase):
    def test_claude_snapshot_newest_file_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            old = cache / "usage-aaaa.json"
            new = cache / "usage-bbbb.json"
            old.write_text(json.dumps({"percent": 90, "resetsAtEpoch": NOW + 100, "model": "Old"}))
            new.write_text(
                json.dumps(
                    {
                        "percent": 17,
                        "resetsAtEpoch": NOW + 2 * 86400,
                        "model": "Fable",
                        "fetchedAtMs": (NOW - 3600) * 1000,
                    }
                )
            )
            os.utime(old, (NOW - 500, NOW - 500))
            os.utime(new, (NOW - 10, NOW - 10))
            entry = dc.read_claude_quota(cache, NOW)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "Claude Code")
        self.assertEqual(entry["window"], "Fable · 7d")
        self.assertEqual(entry["left_percent"], 83)
        self.assertEqual(entry["level"], "ok")
        self.assertEqual(entry["reset_relative"], "2 天 0 小时后重置")
        self.assertEqual(entry["snapshot_age_hours"], 1.0)

    def test_claude_snapshot_missing_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(dc.read_claude_quota(Path(tmp), NOW))

    def test_codex_last_rate_limits_event_in_newest_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            day = sessions / "2026" / "09" / "01"
            day.mkdir(parents=True)
            rollout = day / "rollout-2026-09-01T23-33-59-x.jsonl"
            first = {
                "limit_id": "codex",
                "primary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": NOW + 86400},
                "secondary": None,
                "plan_type": "prolite",
                "rate_limit_reached_type": None,
            }
            last = dict(first, primary=dict(first["primary"], used_percent=33.0))
            lines = [
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": first}}),
                json.dumps({"type": "event_msg", "payload": {"type": "other"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": last}}),
            ]
            rollout.write_text("\n".join(lines) + "\n")
            os.utime(rollout, (NOW - 7200, NOW - 7200))
            entry = dc.read_codex_quota(sessions, NOW)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "Codex")
        self.assertEqual(entry["window"], "prolite · 7d")
        self.assertEqual(entry["used_percent"], 33)
        self.assertEqual(entry["left_percent"], 67)
        self.assertEqual(entry["reset_relative"], "1 天 0 小时后重置")
        self.assertEqual(entry["snapshot_age_hours"], 2.0)

    def test_codex_without_events_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026" / "09" / "01"
            day.mkdir(parents=True)
            (day / "rollout-x.jsonl").write_text('{"type":"event_msg"}\n')
            self.assertIsNone(dc.read_codex_quota(Path(tmp), NOW))


class BuildTests(unittest.TestCase):
    def test_build_degrades_to_warnings_without_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = dc.build(
                date(2026, 9, 2),
                place=None,
                now=NOW,
                claude_cache=Path(tmp) / "nope",
                codex_sessions=Path(tmp) / "nope",
                ov=Path(tmp),
            )
        self.assertEqual(context["schema"], dc.CONTEXT_SCHEMA)
        self.assertEqual(context["quota"], [])
        self.assertIsNone(context["weather"])
        self.assertEqual(len(context["warnings"]), 2)

    def test_weather_failure_is_a_warning_not_an_error(self):
        def boom(place, day, region=None, country=None):
            raise OSError("offline")

        with tempfile.TemporaryDirectory() as tmp:
            context = dc.build(
                date(2026, 9, 2),
                place="Lisbon",
                now=NOW,
                claude_cache=Path(tmp),
                codex_sessions=Path(tmp),
                weather_fetcher=boom,
            )
        self.assertIsNone(context["weather"])
        self.assertTrue(any("weather unavailable" in w for w in context["warnings"]))

    def test_weather_is_passed_through(self):
        def fake(place, day, region=None, country=None):
            return {"place": place, "tmin": 13, "tmax": 25, "summary": "少云", "precip_probability": 2, "hours": [], "date": day.isoformat()}

        with tempfile.TemporaryDirectory() as tmp:
            context = dc.build(
                date(2026, 9, 2),
                place="Lisbon",
                now=NOW,
                claude_cache=Path(tmp),
                codex_sessions=Path(tmp),
                weather_fetcher=fake,
            )
        self.assertEqual(context["weather"]["place"], "Lisbon")
        self.assertIn("Lisbon 13–25°C 少云 降水 2%", dc.text_view(context))


class ForecastSummaryTests(unittest.TestCase):
    def test_summary_keeps_three_anchor_hours(self):
        daily = {
            "temperature_2m_max": [25.1],
            "temperature_2m_min": [13.4],
            "precipitation_probability_max": [2],
            "weather_code": [2],
        }
        hourly = {
            "time": [f"2026-09-02T{h:02d}:00" for h in range(24)],
            "temperature_2m": [float(h) for h in range(24)],
        }
        summary = dc.summarize_forecast(daily, hourly, "Lisbon")
        self.assertEqual((summary["tmin"], summary["tmax"]), (13, 25))
        self.assertEqual(summary["summary"], "少云")
        self.assertEqual([h["hour"] for h in summary["hours"]], [9, 12, 18])


if __name__ == "__main__":
    unittest.main()


class CodexParseRobustnessTests(unittest.TestCase):
    def test_key_order_and_extra_nesting_do_not_matter(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026" / "09" / "01"
            day.mkdir(parents=True)
            rollout = day / "rollout-x.jsonl"
            event = {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "plan_type": "plus",
                        "credits": {"balance": "0", "nested": {"deep": True}},
                        "primary": {"resets_at": NOW + 3600, "window_minutes": 300, "used_percent": 60.0},
                        "limit_id": "codex",
                    },
                },
            }
            rollout.write_text(json.dumps(event, indent=None) + "\n")
            os.utime(rollout, (NOW, NOW))
            entry = dc.read_codex_quota(Path(tmp), NOW)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["window"], "plus · 300m")
        self.assertEqual(entry["left_percent"], 40)
        self.assertEqual(entry["level"], "low")


class ConfigPlaceTests(unittest.TestCase):
    def test_place_falls_back_to_the_private_digest_config(self):
        seen = []

        def fake(place, day, region=None, country=None):
            seen.append((place, region))
            return {"place": place, "tmin": 1, "tmax": 2, "summary": "晴", "precip_probability": 0, "hours": []}

        with tempfile.TemporaryDirectory() as tmp:
            ov = Path(tmp)
            (ov / "_meta").mkdir()
            (ov / "_meta" / "digest.toml").write_text('[weather]\nplace = "Lisbon"\nregion = "Lisboa"\n', encoding="utf-8")
            context = dc.build(date(2026, 9, 2), place=None, now=NOW, claude_cache=ov, codex_sessions=ov, weather_fetcher=fake, ov=ov)
            self.assertEqual(seen, [("Lisbon", "Lisboa")])
            self.assertEqual(context["weather"]["place_source"], "config")
            explicit = dc.build(date(2026, 9, 2), place="Porto", now=NOW, claude_cache=ov, codex_sessions=ov, weather_fetcher=fake, ov=ov)
            self.assertEqual(seen[-1], ("Porto", None))
            self.assertEqual(explicit["weather"]["place_source"], "argument")

    def test_offline_skips_the_fetch_and_says_why(self):
        """A run whose allowlist grants no web access still gets the quota half."""
        calls = []

        def fake(place, day, region=None, country=None):
            calls.append(place)
            return {"place": place, "tmin": 1, "tmax": 2, "summary": "晴", "precip_probability": 0, "hours": []}

        with tempfile.TemporaryDirectory() as tmp:
            ov = Path(tmp)
            (ov / "_meta").mkdir()
            (ov / "_meta" / "digest.toml").write_text('[weather]\nplace = "Lisbon"\n', encoding="utf-8")
            context = dc.build(date(2026, 9, 2), place=None, now=NOW, claude_cache=ov, codex_sessions=ov, weather_fetcher=fake, ov=ov, offline=True)
            self.assertEqual(calls, [])
            self.assertIsNone(context["weather"])
            self.assertTrue(any("--offline" in w for w in context["warnings"]))
            bare = dc.build(date(2026, 9, 2), place=None, now=NOW, claude_cache=ov, codex_sessions=ov / "none", weather_fetcher=fake, ov=ov / "none", offline=True)
            self.assertFalse(any("--offline" in w for w in bare["warnings"]))

    def test_no_place_anywhere_means_no_weather_and_no_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = Path(tmp)
            context = dc.build(date(2026, 9, 2), place=None, now=NOW, claude_cache=ov, codex_sessions=ov, ov=ov)
        self.assertIsNone(context["weather"])
        self.assertFalse(any("weather" in w for w in context["warnings"]))


class GeocodePickTests(unittest.TestCase):
    RESULTS = [
        {"name": "Mountain View", "admin1": "Arkansas", "country_code": "US", "population": 2837},
        {"name": "Mountain View", "admin1": "California", "country_code": "US", "population": 80435},
        {"name": "Mountain View", "admin1": "Hawaii", "country_code": "US", "population": 3924},
    ]

    def test_most_populous_wins_without_a_region(self):
        self.assertEqual(dc.pick_location(self.RESULTS, None, None)["admin1"], "California")

    def test_region_filters_case_insensitively(self):
        self.assertEqual(dc.pick_location(self.RESULTS, "hawaii", None)["admin1"], "Hawaii")
        self.assertIsNone(dc.pick_location(self.RESULTS, "Nevada", None))

    def test_country_filters(self):
        self.assertIsNone(dc.pick_location(self.RESULTS, None, "CA"))
        self.assertEqual(dc.pick_location(self.RESULTS, None, "us")["population"], 80435)
