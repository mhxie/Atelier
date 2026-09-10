"""Daily brief prioritization, degraded-input, CLI, and reconciliation tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

import daily_brief as db  # noqa: E402

TODAY = "2099-01-31"

DEADLINES = """
[meta]
refreshed = 2099-01-30
max_age_days = 10

[[deadline]]
slug = "document-expiry"
label = "Document expires"
due = 2099-02-01
kind = "obligation"
reversible = false
source = "travel/example-trip.md:12"

[[deadline]]
slug = "hotel-credit"
label = "Hotel credit #1"
due = 2099-02-04
kind = "perk"
reversible = false
source = "finance/example-tracker.md:107"
action = "book one standalone night"

[[deadline]]
slug = "far-off"
label = "Way out there"
due = 2100-01-01
kind = "perk"
reversible = false
source = "finance/example-tracker.md:108"

[[deadline]]
slug = "needs-long-lead"
label = "Award night needs a booking"
due = 2099-03-15
kind = "perk"
reversible = false
source = "finance/example-tracker.md:131"
lead_days = 60

[[deadline]]
slug = "probe-midpoint"
label = "环境探针中检"
due = 2099-03-01
kind = "milestone"
reversible = false
source = "travel/example-trip.md:30"
action = "定义性 vs plumbing 的体感"
lead_days = 45

[[deadline]]
slug = "year-end-decision"
label = "年底决策点"
due = 2099-06-30
kind = "milestone"
reversible = true
source = "travel/example-trip.md:31"
lead_days = 30

[[deadline]]
slug = "reversible-chore"
label = "Reversible obligation"
due = 2099-02-02
kind = "obligation"
reversible = true
source = "travel/example-trip.md:20"
"""

RECURRING = """## Home

- rotate-quarterly  every:6mo  last-done:2096-12-21  area:#home
- filter-a  every:1mo  last-done:2098-12-31  area:#home
- check-weekly  every:1w  last-done:2099-01-24  area:#home

## Relationship

- audit-monthly  every:1mo  last-done:2098-10-15  area:#prm
- recently-done  every:6mo  last-done:2099-01-30  area:#home
"""

TODOS = """## Q3

- [ ] 办理 [表单公证](<../people/x.md>)  due:2099-02-01  area:#energy
- [ ] 会议审稿  due:2099-02-05  area:#capacity
- [ ] 远期项目  due:2099-05-01  area:#capacity
- [ ] 无日期项目  area:#capacity
- [x] 已完成的  due:2099-02-01
"""

HEALTH = """# Longitudinal metrics

## Body composition

| Date | Source | Weight (kg) |
|---|---|---|
| [2098-09-11](../daily-notes/2098-09-11.md) | scale | 80 |
| [2098-06-01](../daily-notes/2098-06-01.md) | DEXA | 82 |

## Thyroid

| Date | TSH |
|---|---|
| [2099-01-20](reports/x.md) | 1.0 |
"""

TRACKING = {
    "refreshed_at": "2099-01-31T02:00:00-07:00",
    "anime": {
        "date": "2099-01-31",
        "updates": ["Show A Ep.9 08:00 PDT 已更新", "Show B Ep.3 已更新"],
    },
    "concerts": {
        "date": "2099-01-31",
        "reminders": [{"artist": "Artist One", "sale_date": "2099-02-02", "city": "Seattle"}],
    },
}


def build_vault(root: Path, **parts) -> Path:
    vault = root / "vault"
    for sub in ("_meta", "gtd", "cache", "reflections", "finance", "travel", "health"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    (vault / "finance" / "example-tracker.md").write_text("x\n", encoding="utf-8")
    (vault / "travel" / "example-trip.md").write_text("x\n", encoding="utf-8")

    if parts.get("deadlines", DEADLINES) is not None:
        (vault / "_meta" / "deadlines.toml").write_text(
            parts.get("deadlines", DEADLINES), encoding="utf-8"
        )
    if parts.get("recurring", RECURRING) is not None:
        (vault / "gtd" / "recurring.md").write_text(
            parts.get("recurring", RECURRING), encoding="utf-8"
        )
    if parts.get("todos", TODOS) is not None:
        (vault / "gtd" / "2099Q1.md").write_text(parts.get("todos", TODOS), encoding="utf-8")
    if parts.get("health", HEALTH) is not None:
        (vault / "health" / "metrics.md").write_text(parts.get("health", HEALTH), encoding="utf-8")
    tracking = parts.get("tracking", TRACKING)
    if tracking is not None:
        (vault / "cache" / "reminders.json").write_text(
            tracking if isinstance(tracking, str) else json.dumps(tracking, ensure_ascii=False),
            encoding="utf-8",
        )
        (vault / "_meta" / "brief_sources.toml").write_text(
            '[tracking]\ncache = "cache/reminders.json"\n', encoding="utf-8"
        )
    return vault


class PureFunctionTests(unittest.TestCase):
    def test_days_phrase(self):
        self.assertEqual(db._days_phrase(-3), "逾期 3d")
        self.assertEqual(db._days_phrase(0), "今天")
        self.assertEqual(db._days_phrase(1), "明天")
        self.assertEqual(db._days_phrase(5), "5d")

    def test_effective_today_rolls_back_before_three_am(self):
        self.assertEqual(db.effective_today(datetime(2099, 1, 31, 1, 12)).isoformat(), "2099-01-30")
        self.assertEqual(db.effective_today(datetime(2099, 1, 31, 8, 0)).isoformat(), "2099-01-31")

    def test_clean_todo_text_strips_metadata_and_links(self):
        cleaned = db.clean_todo_text(
            "办理 [表单公证](<../people/x.md>)  due:2099-02-01  area:#energy"
        )
        self.assertNotIn("due:", cleaned)
        self.assertNotIn("area:", cleaned)
        self.assertNotIn("](", cleaned)
        self.assertIn("表单公证", cleaned)

    def test_format_reminder_accepts_strings_and_records(self):
        self.assertEqual(db._format_reminder("Artist Two 9/3 开票"), "Artist Two 9/3 开票")
        self.assertIn("Artist One", db._format_reminder({"artist": "Artist One", "date": "2099-02-02"}))
        self.assertEqual(db._format_reminder(42), "")

    def test_reminder_keeps_the_tail_that_carries_the_decision(self):
        reminder = (
            "Some Artist and Another · A Recital Hall "
            "[背景 · 这是一段故意加长且完全虚构的匹配说明，用来测试末尾行动信息是否保留] "
            "距离演出 14 天，尚未购票；要买票吗？"
        )
        self.assertGreater(len(reminder), db.ITEM_TEXT_CHARS)
        formatted = db._format_reminder(reminder)
        self.assertIn("尚未购票", formatted)
        self.assertIn("14 天", formatted)
        self.assertLessEqual(len(formatted), db.REMINDER_TEXT_CHARS)

    def test_cap_folds_from_the_bottom_tier_up(self):
        groups = [
            db.Group(tier=1, kind="closing", heading="closing 2", items=[db.Item("a"), db.Item("b")]),
            db.Group(tier=2, kind="todo", heading="todo 2", items=[db.Item("c"), db.Item("d")]),
            db.Group(tier=3, kind="recurring", heading="recurring 2", items=[db.Item("e"), db.Item("f")]),
        ]
        capped, folded, over = db.apply_cap(groups, cap=7)
        self.assertEqual(folded, 1)
        self.assertFalse(over)
        self.assertFalse(capped[0].folded)
        self.assertFalse(capped[1].folded)
        self.assertTrue(capped[2].folded)

    def test_tier_one_is_never_folded_by_the_cap(self):
        groups = [
            db.Group(tier=1, kind="closing", heading="c", items=[db.Item("a"), db.Item("b")]),
            db.Group(tier=2, kind="todo", heading="t", items=[db.Item("c"), db.Item("d")]),
            db.Group(tier=3, kind="recurring", heading="r", items=[db.Item("e")]),
        ]
        capped, _, over = db.apply_cap(groups, cap=3)
        tier_one = next(g for g in capped if g.tier == 1)
        self.assertFalse(tier_one.folded)
        self.assertTrue(over)

    def test_tier_three_count_lines_merge_when_folding_is_not_enough(self):
        groups = [
            db.Group(tier=1, kind="closing", heading="c", items=[db.Item("a"), db.Item("b")]),
            db.Group(tier=3, kind="recurring", heading="recurring: 5 条逾期", folded=True),
            db.Group(tier=3, kind="review", heading="review 债 4 项", folded=True),
            db.Group(tier=3, kind="anime", heading="今日更新: X", folded=True),
        ]
        capped, _, over = db.apply_cap(groups, cap=3)
        merged = next(g for g in capped if g.kind == "merged")
        self.assertIn("recurring: 5 条逾期", merged.heading)
        self.assertIn("review 债 4 项", merged.heading)
        self.assertIn("今日更新: X", merged.heading)
        self.assertEqual(sum(g.rendered_lines() for g in capped), 4)
        self.assertTrue(over)

    def test_cap_never_drops_a_group_entirely(self):
        groups = [db.Group(tier=3, kind="x", heading="h", items=[db.Item("a")] * 30)]
        capped, _, over = db.apply_cap(groups, cap=1)
        self.assertEqual(sum(g.rendered_lines() for g in capped), 1)
        self.assertFalse(over)

    def test_folded_group_shows_its_fold_heading(self):
        group = db.Group(
            tier=3, kind="r", heading="short", items=[db.Item("a")], fold_heading="short (see tool)"
        )
        self.assertEqual(group.display_heading(), "short")
        group.folded = True
        self.assertEqual(group.display_heading(), "short (see tool)")


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _run(self, vault: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/daily_brief.py", *args],
            cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)},
            capture_output=True, text=True, timeout=90,
        )

    def _brief(self, *extra: str, vault=None, today=TODAY, cap="40", **parts) -> dict:
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            vault = vault or build_vault(Path(tmp), **parts)
            proc = self._run(vault, "--json", "--today", today, "--skip-cues", "--cap", cap, *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _group(self, brief: dict, kind: str) -> dict:
        group = next((group for group in brief["groups"] if group["kind"] == kind), None)
        self.assertIsNotNone(group, f"no {kind!r} group; got {[g['kind'] for g in brief['groups']]}")
        return group


class BriefIntegrationTests(VaultTests):
    def test_default_brief_prioritizes_action_and_keeps_provenance(self):
        brief = self._brief()
        groups = {group["kind"]: group for group in brief["groups"]}
        now, lead, focus = (groups[kind] for kind in ("closing_now", "closing_lead", "focus"))
        self.assertEqual((brief["schema"], brief["date"]), (db.BRIEF_SCHEMA, TODAY))
        self.assertIn("1 件", now["heading"])
        self.assertIn("Document expires", now["items"][0]["text"])
        self.assertEqual(now["items"][0]["source"], "travel/example-trip.md:12")
        self.assertIn("Hotel credit #1", lead["items"][0]["text"])
        self.assertIn("Award night needs a booking", json.dumps(lead["items"]))
        self.assertEqual((focus["tier"], focus["heading"]), (1, "本季主线 1 件"))
        self.assertEqual(focus["items"][0]["text"], "环境探针中检 · 29d")
        self.assertEqual(focus["items"][0]["label"], "环境探针中检")
        self.assertEqual(focus["items"][0]["source"], "travel/example-trip.md:30")
        self.assertNotIn("hint", focus["items"][0])
        self.assertNotIn("环境探针中检", json.dumps([now, lead], ensure_ascii=False))
        for absent in ("Way out there", "Reversible obligation", "年底决策点", "recently-done"):
            with self.subTest(excluded=absent):
                self.assertNotIn(absent, json.dumps(brief, ensure_ascii=False))
        health = groups["health"]
        self.assertEqual(health["tier"], 3)
        self.assertIn("体重上次 2098-09-11 (142d 前)", health["heading"])
        self.assertNotIn("2099-01-20", health["heading"])
        self.assertEqual(
            brief["signals"], {"closing": 3, "closing_now": 1, "focus_days": 29, "weight_age_days": 142}
        )
        todo_now, todo = groups["todo_now"], groups["todo"]
        self.assertEqual((todo_now["tier"], len(todo_now["items"])), (1, 1))
        self.assertIn("明天", todo_now["items"][0]["text"])
        self.assertNotIn("due:", todo_now["items"][0]["text"])
        self.assertEqual(todo["tier"], 2)
        self.assertEqual([item["days_left"] for item in todo["items"]], [5])
        for absent in ("远期项目", "无日期项目", "已完成的"):
            with self.subTest(todo_excluded=absent):
                self.assertNotIn(absent, json.dumps(todo, ensure_ascii=False))
        recurring = groups["recurring"]
        self.assertIn("逾期", recurring["heading"])
        self.assertIn("check-weekly", json.dumps(recurring["items"]))
        for absent in ("rotate-quarterly", "audit-monthly"):
            self.assertNotIn(absent, json.dumps(recurring["items"]))
        anime, concert = groups["anime"], groups["concert"]
        self.assertTrue(anime["folded"])
        self.assertEqual(anime["items"], [])
        self.assertIn("Show A Ep.9", anime["heading"])
        self.assertIn("Artist One", concert["heading"])
        self.assertEqual((anime["tier"], concert["tier"]), (3, 1))
        tiers = [group["tier"] for group in brief["groups"]]
        self.assertEqual(tiers, sorted(tiers))
        for group in brief["groups"]:
            for item in group["items"]:
                self.assertIn("label", item, item)
                self.assertNotIn("d ·", item["label"])
                if "hint" in item:
                    self.assertTrue(item["text"].endswith(item["hint"]), item)
                    self.assertNotIn(item["hint"], item["label"])
        flagged = {"heading": "h", "items": [{"text": "x", "flag": "待核", "flag_source": "a.md:1"}]}
        self.assertIn("[待核", db.text_view({**brief, "groups": [flagged]}))

    def test_attention_caps_fold_lower_priorities_and_report_overflow(self):
        for cap in (3, 5, 6, 12):
            with self.subTest(cap=cap):
                brief = self._brief("--cap", str(cap))
                self.assertEqual(brief["cap"], cap)
                self.assertFalse(self._group(brief, "closing_now")["folded"])
                self.assertFalse(self._group(brief, "closing_lead")["folded"])
                if cap == 3:
                    kinds = [group["kind"] for group in brief["groups"]]
                    self.assertLess(kinds.index("closing_lead"), kinds.index("focus"))
                    self.assertFalse(self._group(brief, "focus")["folded"])
                    self.assertEqual(len(self._group(brief, "focus")["items"]), 1)
                    self.assertTrue(brief["over_cap"])
                    self.assertTrue(any("over the 3-line cap" in w for w in brief["warnings"]))
                elif cap == 6:
                    now = self._group(brief, "todo_now")
                    self.assertFalse(now["folded"])
                    self.assertEqual(len(now["items"]), 1)
                    self.assertTrue(self._group(brief, "todo")["folded"])
                    self.assertTrue(brief["over_cap"])
                elif cap == 12:
                    self.assertLessEqual(brief["rendered_lines"], 12)
                    self.assertGreater(brief["folded_by_cap"], 0)
                    self.assertFalse(brief["over_cap"])

    def test_missing_and_stale_inputs_remain_explicit(self):
        cases = (
            ("empty health", {"health": "# m\n\n## Body composition\n\n| Date | W |\n|---|---|\n"}, None),
            ("missing health", {"health": None}, "health metrics missing"),
            ("unknown signals", {"deadlines": None, "health": None}, None),
            ("missing deadlines", {"deadlines": None}, "deadline index missing"),
            ("stale deadlines", {"deadlines": DEADLINES.replace("2099-01-30", "2098-07-01")}, "deadline index stale"),
            ("stale tracking", {"tracking": dict(TRACKING, refreshed_at="2099-01-24T02:00:00-07:00")}, "reminder cache stale 7d"),
            ("invalid tracking", {"tracking": "not json at all"}, "reminder cache"),
            ("bad deadline metadata", {"deadlines": DEADLINES.replace('[meta]\nrefreshed = 2099-01-30\nmax_age_days = 10', 'meta = "bad"')}, "schema errors"),
            ("bad recurring date", {"recurring": RECURRING + '\n- broken every:1w last-done:2099-02-30\n'}, "invalid recurring"),
            ("empty vault", dict.fromkeys(("deadlines", "recurring", "todos", "tracking", "health")), None),
        )
        for name, parts, warning in cases:
            with self.subTest(name=name):
                brief = self._brief(**parts)
                if warning:
                    self.assertTrue(any(warning in w for w in brief["warnings"]), brief["warnings"])
                if name == "empty health":
                    self.assertIn("无记录", self._group(brief, "health")["heading"])
                elif name == "missing health":
                    self.assertNotIn("health", [group["kind"] for group in brief["groups"]])
                elif name == "unknown signals":
                    self.assertEqual(brief["signals"], {})
                elif name == "missing deadlines":
                    self.assertIsNotNone(self._group(brief, "todo"))
                elif name == "stale tracking":
                    self.assertIn("缓存 7d 前", self._group(brief, "anime")["heading"])
                elif name == "empty vault":
                    self.assertEqual(brief["groups"], [])
                    self.assertEqual(brief["rendered_lines"], 0)

    def test_tracking_updates_and_failure_keep_the_last_success(self):
        for name in ("followup", "failed refresh"):
            with self.subTest(name=name):
                tracking = json.loads(json.dumps(TRACKING))
                if name == "followup":
                    tracking["followups"] = {"date": TODAY, "updates": ["Followed Sequel 档期更新：未定 → 2099-04-02"]}
                else:
                    tracking["anime"].update(
                        last_success_at="2099-01-30T05:30:00-08:00",
                        failed_at="2099-01-31T05:30:00-08:00", error="URLError: offline",
                    )
                brief = self._brief(tracking=tracking)
                heading = self._group(brief, "anime")["heading"]
                if name == "followup":
                    self.assertIn("Followed Sequel", heading)
                    self.assertIn("动漫更新", heading)
                else:
                    self.assertIn("Show A Ep.9", heading)
                    self.assertTrue(any("anime refresh failed" in w for w in brief["warnings"]))

    def test_malformed_tracking_config_keeps_other_brief_groups(self):
        vault = build_vault(self.root)
        (vault / "_meta/brief_sources.toml").write_text('tracking = "bad"\n')
        brief = self._brief(vault=vault)
        self._group(brief, "todo")
        self.assertNotIn("anime", [group["kind"] for group in brief["groups"]])

    def test_recurring_completion_normalizes_dates_and_survives_readback(self):
        vault = build_vault(self.root)
        path = vault / "gtd/recurring.md"
        def run(*args):
            return subprocess.run([sys.executable, "scripts/recurring.py", *args],
                cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)}, capture_output=True, text=True, timeout=30)
        proc = run("done", "check-weekly", "20990131")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("last-done:2099-01-31", path.read_text())
        before = path.read_text()
        proc = run("done", "check-weekly", "9999-12-31")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(path.read_text(), before)
        path.write_text(before + '\n- broken every:1w last-done:2099-02-30\n')
        proc = run("list", "--all", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("invalid recurring", proc.stderr)
        self.assertIn("check-weekly", [row["slug"] for row in json.loads(proc.stdout)])
        proc = run("done", "broken", "2099-01-31")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("- broken every:1w last-done:2099-01-31", path.read_text())

    def test_recurring_fresh_window_spans_the_due_boundary(self):
        for today, fresh in (("2099-01-31", True), ("2099-01-30", True), ("2099-02-06", False)):
            with self.subTest(today=today):
                group = self._group(self._brief(today=today), "recurring")
                self.assertEqual("check-weekly" in json.dumps(group["items"]), fresh)
                if not fresh:
                    self.assertIn("逾期", group["heading"])


class CliTests(VaultTests):
    def test_cli_formats_output_and_validates_arguments(self):
        vault = build_vault(self.root)
        target = self.root / "brief.json"
        cases = (
            ("default text", ["--today", TODAY, "--skip-cues"], 0),
            ("bad today", ["--today", "tomorrow"], 1),
            ("bad cap", ["--cap", "0"], 1),
            ("cue checks", ["--today", TODAY, "--json"], 0),
            ("write file", ["--today", TODAY, "--skip-cues", "--json", "--out", str(target)], 0),
        )
        for name, args, code in cases:
            with self.subTest(name=name):
                proc = self._run(vault, *args)
                self.assertEqual(proc.returncode, code, proc.stderr)
                if name == "default text":
                    self.assertIn(f"# 今日 {TODAY}", proc.stdout)
                    self.assertNotIn('"schema"', proc.stdout)
                elif name == "bad today":
                    self.assertIn("must be YYYY-MM-DD", proc.stderr)
                elif name == "cue checks":
                    json.loads(proc.stdout)
                elif name == "write file":
                    self.assertEqual(json.loads(target.read_text())["date"], TODAY)


class GlanceLineTests(unittest.TestCase):
    def test_latin_text_is_cut_at_a_word_boundary(self):
        text = "burn on a trip that already exists; do not build a trip for it"
        got = db._truncate(text, 40)
        self.assertTrue(got.endswith("…"), got)
        self.assertNotIn("bui…", got)
        self.assertFalse(got[:-1].rstrip().endswith(" "), got)

    def test_cjk_still_uses_the_full_budget(self):
        text = "落到已有行程别为它造行程再多写一些字撑满这一行的预算"
        got = db._truncate(text, 12)
        self.assertEqual(len(got), 12)

    def test_short_text_is_untouched(self):
        self.assertEqual(db._truncate("Example Hotel 2025 免房券", 96), "Example Hotel 2025 免房券")

    def test_a_bracketed_rationale_is_dropped_but_the_decision_survives(self):
        raw = (
            "演唱会: Some Artist · Some Hall "
            "[反画像 · 用一段很长的理由说明为什么这场值得测试跨类型兴趣] "
            "距离演出 14 天，尚未购票；要买票吗？"
        )
        got = db._format_reminder(raw)
        self.assertNotIn("反画像", got)
        self.assertIn("要买票吗", got)
        self.assertIn("Some Artist", got)

    def test_a_short_bracketed_label_is_kept(self):
        raw = "演唱会: Some Artist [已购票] 明晚"
        self.assertIn("[已购票]", db._format_reminder(raw))


class ReconciliationTests(VaultTests):
    def _item(self, brief: dict, label_start: str) -> dict:
        return next(
            item for group in brief["groups"] for item in group["items"]
            if item.get("label", "").startswith(label_start)
        )

    def _note(self, vault: Path, text: str, at=datetime(2099, 2, 1, 12, 0)) -> Path:
        path = vault / "travel" / "trips" / "example-city.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.utime(path, (at.timestamp(), at.timestamp()))
        return path

    def test_skip_prefixes_follow_the_path_registry(self):
        from unittest import mock

        with mock.patch("_paths.tier_segments", return_value={"archive": "attic", "wiki": "kb", "papers": "papers"}), \
             mock.patch("_paths.wiki_dirs", return_value=[self.root / "kb", self.root / "kb-zh", Path("/elsewhere/kb")]):
            prefixes = db._reconcile_skip_prefixes(self.root)
        self.assertIn("attic", prefixes)
        self.assertNotIn("archive", prefixes)
        self.assertIn("kb-zh", prefixes)
        self.assertIn("cache", prefixes)
        self.assertNotIn("/elsewhere/kb", prefixes)
        with mock.patch("_paths.tier_segments", side_effect=RuntimeError("no registry")):
            self.assertIn("archive", db._reconcile_skip_prefixes(self.root))

    def test_excluded_tiers_and_the_rows_own_source_do_not_flag(self):
        excluded = ("wiki/note.md", "archive/note.md", "inbox/digest/note.md", "cache/note.md")
        for sources in (excluded, ("finance/example-tracker.md",)):
            with self.subTest(sources=sources), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                vault = build_vault(Path(tmp))
                for relative in sources:
                    path = vault / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    text = "Award night needs a booking, expires 2099-03-15\n" * 140 if relative.startswith("finance/") else "Award night (exp 2099-03-15) 顺带在此兑现。\n"
                    path.write_text(text, encoding="utf-8")
                    stamp = datetime(2099, 2, 1, 12, 0).timestamp()
                    os.utime(path, (stamp, stamp))
                self.assertNotIn("flag", self._item(self._brief(vault=vault), "Award night"))

    def test_refresh_cutoff_distinguishes_refresh_from_done_writes(self):
        cases = (
            ("same-day refresh", datetime(2099, 1, 30, 12), False, False, ((9, False), (15, True))),
            ("later index touch", datetime(2099, 2, 3, 8), False, False, ((9, True),)),
            ("same-day done", datetime(2099, 1, 30, 20), True, False, ((9, True),)),
            ("explicit refreshed_at", datetime(2099, 1, 30, 20), True, True, ((9, False), (15, True))),
        )
        for name, index_time, done, explicit, notes in cases:
            with self.subTest(cutoff=name), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                vault = build_vault(Path(tmp))
                index = vault / "_meta" / "deadlines.toml"
                text = DEADLINES
                if done:
                    text = text.replace(
                        'slug = "reversible-chore"',
                        'status = "done"\nresolved = 2099-01-30\nslug = "reversible-chore"',
                    )
                if explicit:
                    text = text.replace("refreshed = 2099-01-30\n", "refreshed = 2099-01-30\nrefreshed_at = 2099-01-30T12:00:00\n")
                index.write_text(text, encoding="utf-8")
                os.utime(index, (index_time.timestamp(), index_time.timestamp()))
                for hour, flagged in notes:
                    with self.subTest(note_hour=hour):
                        self._note(vault, "Award night (exp 2099-03-15) 顺带在此兑现。\n", datetime(2099, 1, 30, hour))
                        brief = self._brief(vault=vault)
                        item = self._item(brief, "Award night")
                        if flagged:
                            self.assertEqual(item["flag"], "待核")
                        else:
                            self.assertNotIn("flag", item)
                        if name == "same-day refresh" and flagged:
                            self.assertTrue(any("2099-01-30 刷新后" in w for w in brief["warnings"]))

    def test_newer_due_date_mentions_include_actionable_provenance(self):
        vault = build_vault(self.root)
        self._note(vault, "## 已锁定\n\nAward night (exp 2099-03-15) 顺带在此兑现。\n")
        brief = self._brief(vault=vault)
        item = self._item(brief, "Award night")
        self.assertEqual((item["flag"], item["flag_source"]), ("待核", "travel/trips/example-city.md:3"))
        self.assertTrue(any("needs-long-lead" in w and "deadlines.py done" in w for w in brief["warnings"]))

    def test_mentions_require_a_recent_label_and_a_complete_date(self):
        cases = (
            ("slash short", "Award night", "Award night booked, was expiring 3/15.\n", True),
            ("slash padded", "Award night", "Award night booked, was expiring 03/15.\n", True),
            ("Chinese padded", "Award night", "Award night 03月15日 到期，已用掉。\n", True),
            ("Chinese short", "Award night", "Award night 3月15日 到期，已用掉。\n", True),
            ("year slash", "Award night", "Award night used, 2099/03/15 deadline gone.\n", True),
            ("lowercase", "Award night", "award night booked 3/15.\n", True),
            ("uppercase", "Award night", "AWARD NIGHT used, 2099-03-15.\n", True),
            ("longer padded day", "Document expires", "Document expires on 02/15.\n", False),
            ("longer month", "Document expires", "Document expires 12/10.\n", False),
            ("longer day", "Document expires", "Document expires 2/10.\n", False),
            ("exact day", "Document expires", "Document expires 2/1, renewed.\n", True),
            ("older note", "Award night", "Award night (exp 2099-03-15) 顺带在此兑现。\n", False),
            ("unrelated label", "Award night", "Something else happens on 2099-03-15.\n", False),
        )
        for name, label, text, flagged in cases:
            with self.subTest(mention=name), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                vault = build_vault(Path(tmp))
                at = datetime(2098, 1, 1) if name == "older note" else datetime(2099, 2, 1, 12)
                self._note(vault, text, at)
                item = self._item(self._brief(vault=vault), label)
                if flagged:
                    self.assertEqual(item["flag"], "待核")
                    self.assertEqual(item["flag_source"], "travel/trips/example-city.md:1")
                else:
                    self.assertNotIn("flag", item)


if __name__ == "__main__":
    unittest.main()
