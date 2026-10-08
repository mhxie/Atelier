"""CodexBar quota boundary, sanitized cache, and unchanged weather outcomes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import daily_context as dc  # noqa: E402
import digest_note as dn  # noqa: E402

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
        }} for provider in ("codex", "claude", "antigravity")]

    def test_configured_sources_cache_privacy_age_and_expiry(self):
        cache = self.root / "quota.json"
        with patch.dict(os.environ, {"ANTHROPIC_ADMIN_KEY": "PRIVATE_TEST_MARKER"}), patch.object(dc.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (json.dumps(self.payload()), None)
            entries, warnings = dc.read_quota(cache, NOW, True)
            self.assertEqual(warnings, [])
            self.assertEqual([(e["name"], e["window"], e["left_percent"]) for e in entries], [
                ("Codex", "5h", 83), ("Codex", "7d", 15), ("Claude Code", "5h", 83), ("Claude Code", "7d", 15),
                ("Antigravity", "Gemini", 83), ("Antigravity", "Claude + GPT", 15)])
            self.assertEqual(popen.call_args.args[0], ["codexbar", "usage", "--format", "json", "--json-only"])
            self.assertNotIn("ANTHROPIC_ADMIN_KEY", popen.call_args.kwargs["env"])
            config = json.loads(Path(popen.call_args.kwargs["env"]["CODEXBAR_CONFIG"]).read_text())
            self.assertFalse(config["hooks"]["enabled"])
            self.assertEqual({p["id"]: p["source"] for p in config["providers"]}, {"codex": "oauth", "claude": "cli", "antigravity": "auto"})
            self.assertTrue(all(p["cookieSource"] == "off" for p in config["providers"]))
            self.assertNotIn("PRIVATE_TEST_MARKER", cache.read_text())
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            os.utime(cache, (NOW, NOW))  # File modification time must not become observation time.
            cached, _ = dc.read_quota(cache, NOW + 3600, False)
            self.assertEqual(cached[0]["snapshot_age_hours"], 2)
            self.assertEqual(cached[0]["reset_relative"], "1 小时后重置")
            expired, warnings = dc.read_quota(cache, NOW + 7200, False)
            self.assertEqual([e["window"] for e in expired], ["7d", "7d", "Claude + GPT"])
            self.assertTrue(warnings)
            popen.assert_called_once()

    def test_failures_are_unknown_and_partial_success_survives(self):
        cache = self.root / "quota.json"
        for failure in ("bad json", "null", "{}", FileNotFoundError(), dc.subprocess.TimeoutExpired("codexbar", 45)):
            with self.subTest(failure=failure), patch.object(dc.subprocess, "Popen") as popen, patch.object(dc, "stop_process_tree") as stop:
                proc = popen.return_value
                proc.communicate.side_effect = [failure, ("", None)] if isinstance(failure, Exception) else None
                proc.communicate.return_value = (failure, None)
                cache.write_text(json.dumps([dict(name="Codex", window="7d", used_percent=17, reset_epoch=NOW + 86400, snapshot_epoch=NOW)]))
                entries, warnings = dc.read_quota(cache, NOW, True)
                self.assertEqual(entries, [])
                self.assertEqual(json.loads(cache.read_text()), [])
                self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
                self.assertEqual(len(warnings), 3)
                if isinstance(failure, dc.subprocess.TimeoutExpired):
                    stop.assert_called_once_with(proc)
        for key, bad in (("usedPercent", True), ("usedPercent", float("nan")), ("usedPercent", 101),
                         ("windowMinutes", 0), ("resetsAt", "2026-09-01T00:00:00"),
                         ("resetsAt", False), ("resetsAt", 0), ("resetsAt", "")):
            with self.subTest(key=key, bad=bad), patch.object(dc.subprocess, "Popen") as popen:
                data = self.payload()
                data[0]["usage"]["primary"][key] = bad
                data[1] = {"provider": "claude", "error": {"message": "PRIVATE_TEST_MARKER"}}
                data[2] = {"provider": "antigravity", "error": {"message": "PRIVATE_TEST_MARKER"}}
                popen.return_value.returncode = 1
                popen.return_value.communicate.return_value = (json.dumps(data), None)
                entries, warnings = dc.read_quota(cache, NOW, True)
                self.assertEqual([(e["name"], e["window"]) for e in entries], [("Codex", "7d")])
                self.assertIn("Claude Code quota: unavailable", warnings)
                self.assertIn("Antigravity quota: unavailable", warnings)
                self.assertNotIn("PRIVATE_TEST_MARKER", json.dumps(warnings) + cache.read_text())

    def test_antigravity_summary_renders_named_windows_without_duplicate_or_unknown_usage(self):
        data = self.payload()
        usage = data[2]["usage"]
        titles = ("Gemini Session", "Gemini Weekly", "Claude + GPT Session", "Claude + GPT Weekly")
        usage["extraRateWindows"] = [
            {"id": "PRIVATE_TEST_MARKER", "title": title, "window": {
                **usage["primary"], "usedPercent": used, "resetDescription": "PRIVATE_TEST_MARKER"}}
            for title, used in zip(titles, (20, 40, 60, 80))]
        usage["extraRateWindows"].append({"title": "Gemini Session", "usageKnown": False,
                                         "window": {**usage["primary"], "usedPercent": 100}})
        cache = self.root / "quota.json"
        with patch.object(dc.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (json.dumps(data), None)
            entries, warnings = dc.read_quota(cache, NOW, True)
        measured = [entry for entry in entries if entry["name"] == "Antigravity"]
        self.assertEqual([(e["window"], e["left_percent"]) for e in measured], list(zip(titles, (80, 60, 40, 20))))
        self.assertEqual(warnings, [])
        document = "\n\n".join(dn._masthead({"quota": measured}, "2026-09-02"))
        self.assertIn("| 额度 | Antigravity · Gemini Session |", document)
        self.assertIn("Antigravity · Claude + GPT Weekly |", document)
        self.assertIn("| 80% / 2h | 60% / 2h | **40**% / 2h | ==20==% / 2h |", document)
        self.assertNotIn("PRIVATE_TEST_MARKER", document + cache.read_text())
        for row in usage["extraRateWindows"]:
            row["usageKnown"] = False
        with patch.object(dc.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (json.dumps(data), None)
            entries, warnings = dc.read_quota(cache, NOW, True)
        self.assertEqual({e["name"] for e in entries}, {"Codex", "Claude Code"})
        self.assertEqual(warnings, ["Antigravity quota: unavailable"])

    def test_antigravity_legacy_unknown_cadence_and_reset_are_bounded(self):
        data = self.payload()
        usage = data[2]["usage"]
        for key in ("primary", "secondary"):
            usage[key]["windowMinutes"] = None
            usage[key]["resetsAt"] = None
        cache = self.root / "quota.json"
        with patch.object(dc.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (json.dumps(data), None)
            entries, warnings = dc.read_quota(cache, NOW, True)
        measured = [entry for entry in entries if entry["name"] == "Antigravity"]
        self.assertEqual([(e["window"], e["left_percent"], e["reset_epoch"]) for e in measured],
                         [("Gemini", 83, None), ("Claude + GPT", 15, None)])
        self.assertEqual(warnings, [])
        text = dc.text_view({"date": "2026-09-02", "quota": measured})
        self.assertIn("Antigravity (Gemini) 剩 83% [ok] 重置时间未知", text)
        self.assertNotIn("5h", text)
        fresh, _ = dc.read_quota(cache, NOW + 22 * 3600, False)
        expired, warnings = dc.read_quota(cache, NOW + 23 * 3600, False)
        self.assertEqual([e["name"] for e in fresh], ["Codex", "Claude Code", "Antigravity", "Antigravity"])
        self.assertEqual([e["name"] for e in expired], ["Codex", "Claude Code"])
        self.assertIn("Antigravity quota: unavailable", warnings)

    def test_fresh_cli_usage_without_reset_renders_and_expires_without_guessing(self):
        data = self.payload()
        for provider in data[:2]:
            provider["usage"]["primary"].update(usedPercent=0, resetsAt=None)
        cache = self.root / "quota.json"
        with patch.object(dc.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = (json.dumps(data), None)
            entries, warnings = dc.read_quota(cache, NOW, True)
        measured = [e for e in entries if e["window"] == "5h"]
        self.assertEqual([(e["name"], e["left_percent"], e["reset_epoch"]) for e in measured],
                         [("Codex", 100, None), ("Claude Code", 100, None)])
        self.assertEqual(warnings, [])
        document = "\n\n".join(dn._masthead({"quota": measured}, "2026-09-02"))
        self.assertIn("100% / 未知", document)
        fresh, _ = dc.read_quota(cache, NOW + 22 * 3600, False)
        expired, warnings = dc.read_quota(cache, NOW + 23 * 3600, False)
        self.assertEqual(sum(e["window"] == "5h" for e in fresh), 2)
        self.assertNotIn("5h", [e["window"] for e in expired])
        self.assertIn("quota: invalid or expired window omitted", warnings)

    def test_quota_timeout_stops_a_child_in_a_separate_process_group(self):
        for leader_exits in (False, True):
            with self.subTest(leader_exits=leader_exits):
                self._quota_timeout_stops_child(leader_exits)

    def _quota_timeout_stops_child(self, leader_exits):
        child_pid = self.root / "child.pid"
        fake = self.root / "codexbar"
        fake.write_text(f"#!{sys.executable}\nimport subprocess, sys, time\n"
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
                        f"open({str(child_pid)!r}, 'w').write(str(child.pid))\n"
                        + ("" if leader_exits else "time.sleep(60)\n"))
        fake.chmod(0o700)
        launch = subprocess.Popen
        processes = []
        def short_deadline(*args, **kwargs):
            proc = launch(*args, **kwargs)
            if args[0][0] != "codexbar":
                return proc
            processes.append(proc)
            communicate = proc.communicate
            proc.communicate = lambda timeout=None: communicate(timeout=0.5 if timeout == 45 else timeout)
            return proc
        try:
            with patch.dict(os.environ, {"PATH": str(self.root)}), patch.object(dc.subprocess, "Popen", side_effect=short_deadline):
                with self.assertRaises(subprocess.TimeoutExpired):
                    dc._codexbar_rows()
            pid = int(child_pid.read_text())
            state = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), f"quota child survived: {state}")
            self.assertIsNotNone(processes[0].returncode)
        finally:
            for proc in processes:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            if child_pid.exists():
                try:
                    os.kill(int(child_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

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

    def test_opting_out_of_weather_is_silent_but_offline_still_warns(self):
        """A caller that asks for no weather is not missing it; only --offline explains a skip."""
        (self.root / "_meta").mkdir()
        (self.root / "_meta/digest.toml").write_text('[weather]\nplace = "Lisbon"\n')
        calls = []  # build() turns a fetcher exception into a warning, so record calls rather than fail in one
        cases = (
            ("explicit no_weather", {}, {"no_weather": True}, False),
            ("scheduled profile", {"ATELIER_ROUTINE_PROFILE": "test"}, {}, False),
            ("offline", {}, {"offline": True}, True),
        )
        for name, env, options, warned in cases:
            with self.subTest(case=name), patch.dict(os.environ, env, clear=True):
                calls.clear()
                context = dc.build(date(2026, 9, 2), place=None, ov=self.root, now=NOW,
                                   weather_fetcher=lambda *args: calls.append(args), **options)
                self.assertEqual(calls, [])
                self.assertIsNone(context["weather"])
                weather = [warning for warning in context["warnings"] if "weather" in warning]
                self.assertEqual(weather, ["weather skipped for 'Lisbon': --offline"] if warned else [])

    def test_the_digest_skill_reads_context_without_weather(self):
        """The /digest collect step reads the cached quota and skips weather, so it never warns that weather is off."""
        skill = (Path(__file__).resolve().parents[1] / "skills/digest/SKILL.md").read_text(encoding="utf-8")
        self.assertIn('scripts/daily_context.py --no-weather --date "$DAY" --json', skill)
        self.assertNotIn("daily_context.py --offline", skill)


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
