"""CodexBar quota boundary, sanitized cache, and unchanged weather outcomes."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import daily_context as dc  # noqa: E402

NOW = 1_788_400_000.0  # arbitrary fixed clock


class QuotaColourTests(unittest.TestCase):
    def test_level_thresholds_are_on_the_remaining_share(self):
        for left, level in ((83, "ok"), (41, "ok"), (40, "low"), (21, "low"), (20, "critical"), (0, "critical")):
            with self.subTest(left_percent=left):
                self.assertEqual(dc.quota_level(left), level)

    def test_relative_reset_counts_down_in_days_and_hours(self):
        cases = (
            (2 * 86400 + 3 * 3600 + 59, "2 天 3 小时后重置"),
            (5 * 3600, "5 小时后重置"),
            (20 * 60, "20 分钟后重置"),
            (-10, "0 分钟后重置"),
        )
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(dc.relative_reset(NOW + seconds, NOW), expected)


class LocalContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def payload(self):
        def stamp(epoch):
            return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        return [{"provider": provider, "identity": "PRIVATE_TEST_MARKER", "usage": {
            "updatedAt": stamp(NOW - 3600),
            "primary": {"usedPercent": 17, "windowMinutes": 300, "resetsAt": stamp(NOW + 7200)},
            "secondary": {"usedPercent": 85, "windowMinutes": 10080, "resetsAt": stamp(NOW + 86400)},
        }} for provider in ("codex", "claude")]

    def test_oauth_contract_cache_privacy_age_and_expiry(self):
        cache = self.root / "quota.json"
        with patch.dict(os.environ, {"ANTHROPIC_ADMIN_KEY": "PRIVATE_TEST_MARKER"}), patch.object(dc.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (json.dumps(self.payload()), None)
            entries, warnings = dc.read_quota(cache, NOW, True)
            self.assertEqual(warnings, [])
            self.assertEqual([(e["name"], e["window"], e["left_percent"]) for e in entries], [
                ("Codex", "5h", 83), ("Codex", "7d", 15), ("Claude Code", "5h", 83), ("Claude Code", "7d", 15)])
            self.assertEqual(popen.call_args.args[0], ["codexbar", "usage", "--provider", "both", "--source", "oauth", "--format", "json", "--json-only"])
            self.assertNotIn("ANTHROPIC_ADMIN_KEY", popen.call_args.kwargs["env"])
            config = json.loads(Path(popen.call_args.kwargs["env"]["CODEXBAR_CONFIG"]).read_text())
            self.assertFalse(config["hooks"]["enabled"])
            self.assertTrue(all(p["cookieSource"] == "off" for p in config["providers"]))
            self.assertNotIn("PRIVATE_TEST_MARKER", cache.read_text())
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            os.utime(cache, (NOW, NOW))  # File modification time must not become observation time.
            cached, _ = dc.read_quota(cache, NOW + 3600, False)
            self.assertEqual(cached[0]["snapshot_age_hours"], 2)
            self.assertEqual(cached[0]["reset_relative"], "1 小时后重置")
            expired, warnings = dc.read_quota(cache, NOW + 7200, False)
            self.assertEqual([e["window"] for e in expired], ["7d", "7d"])
            self.assertTrue(warnings)
            popen.assert_called_once()

    def test_failures_are_unknown_and_partial_success_survives(self):
        cache = self.root / "quota.json"
        for failure in ("bad json", "null", "{}", FileNotFoundError(), dc.subprocess.TimeoutExpired("codexbar", 45)):
            with self.subTest(failure=failure), patch.object(dc.subprocess, "Popen") as popen, patch.object(dc.os, "killpg") as kill:
                proc = popen.return_value
                proc.communicate.side_effect = [failure, ("", None)] if isinstance(failure, Exception) else None
                proc.communicate.return_value = (failure, None)
                cache.write_text(json.dumps([dict(name="Codex", window="7d", used_percent=17, reset_epoch=NOW + 86400, snapshot_epoch=NOW)]))
                entries, warnings = dc.read_quota(cache, NOW, True)
                self.assertEqual(entries, [])
                self.assertEqual(json.loads(cache.read_text()), [])
                self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
                self.assertEqual(len(warnings), 2)
                if isinstance(failure, dc.subprocess.TimeoutExpired):
                    kill.assert_called_once_with(proc.pid, dc.signal.SIGKILL)
        for key, bad in (("usedPercent", True), ("usedPercent", float("nan")), ("usedPercent", 101),
                         ("windowMinutes", 0), ("resetsAt", "2026-09-01T00:00:00")):
            with self.subTest(key=key, bad=bad), patch.object(dc.subprocess, "Popen") as popen:
                data = self.payload()
                data[0]["usage"]["primary"][key] = bad
                data[1] = {"provider": "claude", "error": {"message": "PRIVATE_TEST_MARKER"}}
                popen.return_value.returncode = 1
                popen.return_value.communicate.return_value = (json.dumps(data), None)
                entries, warnings = dc.read_quota(cache, NOW, True)
                self.assertEqual([(e["name"], e["window"]) for e in entries], [("Codex", "7d")])
                self.assertIn("Claude Code quota: unavailable", warnings)
                self.assertNotIn("PRIVATE_TEST_MARKER", json.dumps(warnings) + cache.read_text())

    def test_refresh_permission_and_offline_are_independent_of_weather(self):
        for profile, permissions, offline, live in ((False, "", False, True), (True, "", False, False),
                (True, "quota:read-extra", False, False), (True, "vault:read-write,quota:read", False, True),
                (True, "quota:read", True, False)):
            env = {"ATELIER_ROUTINE_PROFILE": "test", "ATELIER_ROUTINE_PERMISSIONS": permissions} if profile else {}
            with self.subTest(env=env, offline=offline), patch.dict(os.environ, env, clear=True), patch.object(dc, "_codexbar_rows", return_value=[]) as reader:
                context = dc.build(date(2026, 9, 2), place="Lisbon", ov=self.root, now=NOW, offline=offline,
                                   no_weather=not profile, refresh_quota=True, weather_fetcher=lambda *args: self.fail("weather network"))
                self.assertEqual(reader.called, live)
                self.assertIsNone(context["weather"])

    def test_weather_config_override_failure_and_offline(self):
        for name, place, configured, offline in (
            ("missing", None, False, False), ("failure", "Lisbon", False, False),
            ("explicit", "Lisbon", False, False), ("configured", None, True, False),
            ("offline", None, True, True),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root, calls = Path(tmp), []
                if configured:
                    (root / "_meta").mkdir()
                    (root / "_meta/digest.toml").write_text('[weather]\nplace = "Lisbon"\nregion = "Lisboa"\n')
                def fetch(place, day, region, country):
                    calls.append((place, region))
                    if name == "failure":
                        raise OSError("weather failure")
                    return dict(place=place, tmin=13, tmax=25, summary="少云", precip_probability=2, hours=[])
                kwargs = dict(place=place, now=NOW, offline=offline, ov=root, weather_fetcher=fetch)
                context = dc.build(date(2026, 9, 2), **kwargs)
                self.assertEqual((context["schema"], context["quota"]), (dc.CONTEXT_SCHEMA, []))
                expected = [] if offline or name == "missing" else [("Lisbon", "Lisboa" if configured else None)]
                self.assertEqual(calls, expected)
                if calls and name != "failure":
                    self.assertIn("Lisbon 13–25°C 少云 降水 2%", dc.text_view(context))
                    self.assertEqual(context["weather"]["place_source"], "config" if configured else "argument")
                else:
                    self.assertIsNone(context["weather"])
                warnings = " ".join(context["warnings"])
                self.assertEqual("weather unavailable" in warnings, name == "failure")
                self.assertEqual("--offline" in warnings, offline)
                if configured:
                    override = dc.build(date(2026, 9, 2), **{**kwargs, "place": "Porto", "offline": False})
                    self.assertEqual(calls[-1], ("Porto", None))
                    self.assertEqual(override["weather"]["place_source"], "argument")


class ForecastSummaryTests(unittest.TestCase):
    def test_missing_measurements_never_become_a_zero_clear_forecast(self):
        good = dict(temperature_2m_max=[25], temperature_2m_min=[13],
                    precipitation_probability_max=[0], weather_code=[0])
        for key in good:
            for bad in (None, [], [None]):
                with self.subTest(key=key, bad=bad), tempfile.TemporaryDirectory() as tmp:
                    daily = {**good, key: bad}
                    with patch.object(dc, "_get_json", side_effect=[
                        {"results": [{"name": "Fixture Town", "latitude": 0, "longitude": 0}]},
                        {"daily": daily},
                    ]):
                        context = dc.build(date(2099, 1, 31), place="Fixture Town", ov=Path(tmp))
                    self.assertIsNone(context["weather"])
                    self.assertTrue(any("weather unavailable" in w for w in context["warnings"]))

    def test_summary_keeps_three_anchor_hours(self):
        daily = {
            "temperature_2m_max": [25.1], "temperature_2m_min": [13.4],
            "precipitation_probability_max": [2], "weather_code": [2],
        }
        hourly = {
            "time": [f"2026-09-02T{h:02d}:00" for h in range(24)],
            "temperature_2m": [float(h) for h in range(24)],
        }
        summary = dc.summarize_forecast(daily, hourly, "Lisbon")
        self.assertEqual((summary["tmin"], summary["tmax"]), (13, 25))
        self.assertEqual(summary["summary"], "少云")
        self.assertEqual([h["hour"] for h in summary["hours"]], [9, 12, 18])

    def test_geocoding_selects_population_and_respects_region_and_country(self):
        results = [
            {"name": "Mountain View", "admin1": "Arkansas", "country_code": "US", "population": 2837},
            {"name": "Mountain View", "admin1": "California", "country_code": "US", "population": 80435},
            {"name": "Mountain View", "admin1": "Hawaii", "country_code": "US", "population": 3924},
        ]
        cases = (
            ("population", None, None, "California"),
            ("region case", "hawaii", None, "Hawaii"),
            ("absent region", "Nevada", None, None),
            ("wrong country", None, "CA", None),
            ("country case", None, "us", "California"),
        )
        for name, region, country, expected in cases:
            with self.subTest(location=name):
                picked = dc.pick_location(results, region, country)
                if expected is None:
                    self.assertIsNone(picked)
                else:
                    self.assertEqual(picked["admin1"], expected)
                    if country == "us":
                        self.assertEqual(picked["population"], 80435)


if __name__ == "__main__":
    unittest.main()
