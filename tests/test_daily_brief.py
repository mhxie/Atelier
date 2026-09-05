"""Tests for scripts/daily_brief.py.

What these pin is the triage rule, because the rule is the product. The brief's
value is not that it shows things, it is that it refuses to show most things:

  - long-overdue items fold to a count; only what came due around now itemizes
  - the line cap folds from the bottom tier up, never from the top
  - a missing or stale input produces a warning and a complete brief, because a
    blank morning screen and a confidently stale one are both worse than an
    admitted gap

Integration cases run through subprocess: `todos.py` resolves its vault
directories into module-level constants at import time, so an in-process test
that swapped $OV afterwards would silently read the real vault.
"""

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
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

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

# One long-overdue item, three that came due around now, one satisfied.
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

    def test_clean_todo_text_drops_the_dangling_separator(self):
        self.assertEqual(db.clean_todo_text("交割后归档文件 — 见 [交易记录](<../a.md>)  due:2099-12-31"),
                         "交割后归档文件 — 见 交易记录")

    def test_format_reminder_accepts_strings_and_records(self):
        self.assertEqual(db._format_reminder("Artist Two 9/3 开票"), "Artist Two 9/3 开票")
        self.assertIn("Artist One", db._format_reminder({"artist": "Artist One", "date": "2099-02-02"}))
        self.assertEqual(db._format_reminder(42), "")

    def test_reminder_keeps_the_tail_that_carries_the_decision(self):
        """Long reminders can put the action at the end, past the item width.

        The feed emits one sentence like "<artist> · <venue> [long context]
        距离演出 14 天，尚未购票；要买票吗？". Truncating at
        ITEM_TEXT_CHARS kept the flavour text and cut the days-left and
        not-bought-yet, which is the only part that can change a decision.
        """
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
        """Forfeitable items are the reason the screen exists; the cap cannot hide them."""
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
        # Three count lines became one; the tier-1 group's 3 lines are
        # untouchable, so 4 is the floor and the cap honestly overflows.
        self.assertEqual(sum(g.rendered_lines() for g in capped), 4)
        self.assertTrue(over)

    def test_cap_never_drops_a_group_entirely(self):
        groups = [db.Group(tier=3, kind="x", heading="h", items=[db.Item("a")] * 30)]
        capped, _, over = db.apply_cap(groups, cap=1)
        self.assertEqual(len(capped), 1)
        self.assertEqual(sum(g.rendered_lines() for g in capped), 1)
        self.assertFalse(over)

    def test_folded_group_shows_its_fold_heading(self):
        group = db.Group(
            tier=3, kind="r", heading="short", items=[db.Item("a")], fold_heading="short (see tool)"
        )
        self.assertEqual(group.display_heading(), "short")
        group.folded = True
        self.assertEqual(group.display_heading(), "short (see tool)")


class BriefIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _brief(self, *extra: str, **parts) -> dict:
        vault = build_vault(Path(self.tmp.name), **parts)
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/daily_brief.py",
                "--json",
                "--today",
                parts.get("today", TODAY),
                "--skip-cues",
                "--cap",
                parts.get("cap", "40"),
                *extra,
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "OV": str(vault)},
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _group(self, brief: dict, kind: str) -> dict:
        for group in brief["groups"]:
            if group["kind"] == kind:
                return group
        raise AssertionError(
            f"no {kind!r} group; got {[g['kind'] for g in brief['groups']]}"
        )

    def test_forfeitable_items_split_into_now_and_this_week(self):
        brief = self._brief()
        now = self._group(brief, "closing_now")
        week = self._group(brief, "closing_lead")
        self.assertIn("1 件", now["heading"])
        self.assertIn("Document expires", now["items"][0]["text"])
        self.assertIn("Hotel credit #1", week["items"][0]["text"])

    def test_long_lead_row_surfaces_well_before_its_due_date(self):
        """A 43-day-out award night with lead_days=60 must not wait for week one."""
        brief = self._brief()
        texts = json.dumps(self._group(brief, "closing_lead")["items"], ensure_ascii=False)
        self.assertIn("Award night needs a booking", texts)

    def test_far_off_and_reversible_rows_stay_off_the_screen(self):
        brief = self._brief()
        text = json.dumps(brief, ensure_ascii=False)
        self.assertNotIn("Way out there", text)
        self.assertNotIn("Reversible obligation", text)

    def test_closing_items_carry_their_provenance(self):
        brief = self._brief()
        self.assertEqual(
            self._group(brief, "closing_now")["items"][0]["source"], "travel/example-trip.md:12"
        )

    def test_milestones_in_lead_get_the_focus_slot_not_the_closing_one(self):
        """The quarter's main line: itemized at tier 1 under 本季主线, never
        mistaken for a forfeitable perk even when marked irreversible."""
        brief = self._brief()
        focus = self._group(brief, "focus")
        self.assertEqual(focus["tier"], 1)
        self.assertEqual(focus["heading"], "本季主线 1 件")
        # The milestone's `action` is a description of the quarter's work, not
        # a step for today, so it stays in the index: the row is label + days.
        self.assertEqual(focus["items"][0]["text"], "环境探针中检 · 29d")
        self.assertEqual(focus["items"][0]["label"], "环境探针中检")
        self.assertNotIn("hint", focus["items"][0])
        self.assertEqual(focus["items"][0]["source"], "travel/example-trip.md:30")
        closing = json.dumps(
            [self._group(brief, k) for k in ("closing_now", "closing_lead")], ensure_ascii=False
        )
        self.assertNotIn("环境探针中检", closing)
        self.assertNotIn("年底决策点", json.dumps(brief, ensure_ascii=False))

    def test_focus_slot_sits_below_closing_and_survives_the_cap(self):
        brief = self._brief(cap="3")
        kinds = [g["kind"] for g in brief["groups"]]
        self.assertLess(kinds.index("closing_lead"), kinds.index("focus"))
        self.assertFalse(self._group(brief, "focus")["folded"])
        self.assertEqual(len(self._group(brief, "focus")["items"]), 1)

    def test_health_line_reports_days_since_the_newest_weight_row(self):
        """Guard for the 2026-09-01 digest, which carried no user-health line
        while directions.md marked health observability immediate."""
        brief = self._brief()
        health = self._group(brief, "health")
        self.assertEqual(health["tier"], 3)
        self.assertIn("体重上次 2098-09-11 (142d 前)", health["heading"])
        self.assertNotIn("2099-01-20", health["heading"])  # thyroid is not weight

    def test_health_line_says_so_when_the_table_is_empty(self):
        empty = self._brief(health="# m\n\n## Body composition\n\n| Date | W |\n|---|---|\n")
        self.assertIn("无记录", self._group(empty, "health")["heading"])

    def test_missing_health_metrics_is_a_warning_not_silence(self):
        missing = self._brief(health=None)
        self.assertNotIn("health", [g["kind"] for g in missing["groups"]])
        self.assertTrue(any("health metrics missing" in w for w in missing["warnings"]))

    def test_signals_carry_the_masthead_numbers(self):
        brief = self._brief()
        self.assertEqual(
            brief["signals"],
            {"closing": 3, "closing_now": 1, "focus_days": 29, "weight_age_days": 142},
        )

    def test_signals_are_absent_not_zero_when_unknown(self):
        bare = self._brief(deadlines=None, health=None)
        self.assertEqual(bare["signals"], {})

    def test_dated_todos_inside_the_horizon_itemize(self):
        brief = self._brief()
        now = self._group(brief, "todo_now")
        self.assertEqual(now["tier"], 1)
        self.assertEqual(len(now["items"]), 1)
        self.assertIn("明天", now["items"][0]["text"])
        self.assertNotIn("due:", now["items"][0]["text"])
        todo = self._group(brief, "todo")
        self.assertEqual(todo["tier"], 2)
        self.assertEqual([i["days_left"] for i in todo["items"]], [5])

    def test_a_todo_whose_day_has_arrived_survives_the_cap(self):
        """An overdue dated TODO and a due-today one were folded behind a
        count line while unrelated far-off milestones held the screen. Tier 1
        is never folded, so the split fixes that."""
        brief = self._brief(cap="6")
        self.assertTrue(brief["over_cap"])
        now = self._group(brief, "todo_now")
        self.assertFalse(now["folded"])
        self.assertEqual(len(now["items"]), 1)
        self.assertTrue(self._group(brief, "todo")["folded"])

    def test_undated_far_future_and_done_todos_are_excluded(self):
        brief = self._brief()
        text = json.dumps(self._group(brief, "todo"), ensure_ascii=False)
        self.assertNotIn("远期项目", text)
        self.assertNotIn("无日期项目", text)
        self.assertNotIn("已完成的", text)

    def test_long_overdue_recurring_folds_and_fresh_itemizes(self):
        """The core rule: 804 days overdue is a count, not three lines."""
        brief = self._brief()
        group = self._group(brief, "recurring")
        self.assertIn("逾期", group["heading"])
        itemized = json.dumps(group["items"], ensure_ascii=False)
        self.assertNotIn("rotate-quarterly", itemized)
        self.assertNotIn("audit-monthly", itemized)
        self.assertIn("check-weekly", itemized)

    def test_satisfied_recurring_never_appears(self):
        brief = self._brief()
        self.assertNotIn("recently-done", json.dumps(brief, ensure_ascii=False))

    def test_anime_and_concert_lines_are_single_folded_lines(self):
        brief = self._brief()
        anime = self._group(brief, "anime")
        concert = self._group(brief, "concert")
        self.assertTrue(anime["folded"])
        self.assertEqual(anime["items"], [])
        self.assertIn("Show A Ep.9", anime["heading"])
        self.assertIn("Artist One", concert["heading"])

    def test_followup_changes_join_the_anime_line(self):
        tracking = json.loads(json.dumps(TRACKING))
        tracking["followups"] = {
            "date": TODAY,
            "updates": ["Followed Sequel 档期更新：未定 → 2099-04-02"],
        }
        brief = self._brief(tracking=tracking)
        anime = self._group(brief, "anime")
        self.assertIn("Followed Sequel", anime["heading"])
        self.assertIn("动漫更新", anime["heading"])

    def test_tracking_failure_warns_without_dropping_last_success(self):
        tracking = json.loads(json.dumps(TRACKING))
        tracking["anime"]["last_success_at"] = "2099-01-30T05:30:00-08:00"
        tracking["anime"]["failed_at"] = "2099-01-31T05:30:00-08:00"
        tracking["anime"]["error"] = "URLError: offline"
        brief = self._brief(tracking=tracking)
        self.assertIn("Show A Ep.9", self._group(brief, "anime")["heading"])
        self.assertTrue(any("anime refresh failed" in w for w in brief["warnings"]))

    def test_concert_line_outranks_the_browsable_lines(self):
        """A ticket sale is forfeitable, so it sits with tier 1."""
        brief = self._brief()
        self.assertEqual(self._group(brief, "concert")["tier"], 1)
        self.assertEqual(self._group(brief, "anime")["tier"], 3)

    def test_groups_are_ordered_by_tier(self):
        brief = self._brief()
        tiers = [g["tier"] for g in brief["groups"]]
        self.assertEqual(tiers, sorted(tiers))

    def test_missing_deadline_index_warns_and_keeps_the_rest(self):
        brief = self._brief(deadlines=None)
        self.assertTrue(any("deadline index missing" in w for w in brief["warnings"]))
        self.assertIsNotNone(self._group(brief, "todo"))

    def test_stale_deadline_index_warns_with_the_lag(self):
        stale = DEADLINES.replace("refreshed = 2099-01-30", "refreshed = 2098-07-01")
        brief = self._brief(deadlines=stale)
        self.assertTrue(any("deadline index stale" in w for w in brief["warnings"]))

    def test_stale_tracking_cache_is_marked_on_the_line(self):
        stale = dict(TRACKING)
        stale["refreshed_at"] = "2099-01-24T02:00:00-07:00"
        brief = self._brief(tracking=stale)
        self.assertTrue(any("reminder cache stale 7d" in w for w in brief["warnings"]))
        self.assertIn("缓存 7d 前", self._group(brief, "anime")["heading"])

    def test_unreadable_tracking_cache_warns(self):
        brief = self._brief(tracking="not json at all")
        self.assertTrue(any("reminder cache" in w for w in brief["warnings"]))

    def test_empty_vault_produces_an_empty_brief_not_a_crash(self):
        brief = self._brief(deadlines=None, recurring=None, todos=None, tracking=None, health=None)
        self.assertEqual(brief["groups"], [])
        self.assertEqual(brief["rendered_lines"], 0)

    def test_cap_is_reported_and_respected(self):
        # Tier 1 is 9 lines in this fixture (two closing groups, 本季主线, and
        # the due-tomorrow TODO), so a cap of 12 leaves room for exactly the
        # folded count lines.
        brief = self._brief("--cap", "12")
        self.assertEqual(brief["cap"], 12)
        self.assertLessEqual(brief["rendered_lines"], 12)
        self.assertGreater(brief["folded_by_cap"], 0)
        self.assertFalse(brief["over_cap"])

    def test_cap_preserves_the_forfeitable_group(self):
        brief = self._brief("--cap", "5")
        self.assertFalse(self._group(brief, "closing_now")["folded"])
        self.assertFalse(self._group(brief, "closing_lead")["folded"])

    def test_unachievable_cap_overflows_and_says_so(self):
        brief = self._brief("--cap", "3")
        self.assertTrue(brief["over_cap"])
        self.assertTrue(any("over the 3-line cap" in w for w in brief["warnings"]))
        self.assertFalse(self._group(brief, "closing_now")["folded"])

    def test_schema_and_date_are_declared(self):
        brief = self._brief()
        self.assertEqual(brief["schema"], db.BRIEF_SCHEMA)
        self.assertEqual(brief["date"], TODAY)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/daily_brief.py", *args],
            cwd=REPO_ROOT,
            env={**os.environ, "OV": str(self.vault)},
            capture_output=True,
            text=True,
            timeout=90,
        )

    def test_text_view_is_the_default(self):
        proc = self._run("--today", TODAY, "--skip-cues")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"# 今日 {TODAY}", proc.stdout)
        self.assertNotIn('"schema"', proc.stdout)

    def test_bad_today_is_rejected(self):
        proc = self._run("--today", "tomorrow")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("must be YYYY-MM-DD", proc.stderr)

    def test_bad_cap_is_rejected(self):
        proc = self._run("--cap", "0")
        self.assertEqual(proc.returncode, 1)

    def test_cue_checks_do_not_crash_on_a_bare_vault(self):
        proc = self._run("--today", TODAY, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        json.loads(proc.stdout)

    def test_out_writes_a_file(self):
        target = Path(self.tmp.name) / "brief.json"
        proc = self._run("--today", TODAY, "--skip-cues", "--json", "--out", str(target))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(target.read_text())["date"], TODAY)


class RecurringWindowTests(unittest.TestCase):
    """The fresh window spans both sides of the due boundary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _headings(self, today: str) -> dict:
        vault = build_vault(Path(self.tmp.name))
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/daily_brief.py",
                "--json",
                "--today",
                today,
                "--skip-cues",
                "--cap",
                "40",
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "OV": str(vault)},
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        brief = json.loads(proc.stdout)
        return next(g for g in brief["groups"] if g["kind"] == "recurring")

    def test_item_due_today_counts_as_fresh(self):
        # check-weekly: last-done 2099-01-24, every:1w -> due 2099-01-31
        group = self._headings("2099-01-31")
        self.assertIn("check-weekly", json.dumps(group["items"], ensure_ascii=False))

    def test_item_due_tomorrow_counts_as_fresh(self):
        group = self._headings("2099-01-30")
        self.assertIn("check-weekly", json.dumps(group["items"], ensure_ascii=False))

    def test_item_overdue_beyond_the_window_folds_away(self):
        group = self._headings("2099-02-06")
        self.assertNotIn("check-weekly", json.dumps(group["items"], ensure_ascii=False))
        self.assertIn("逾期", group["heading"])




class GlanceLineTests(unittest.TestCase):
    """Above the fold, a line that needs reconstructing has already failed.

    Two shapes cost more than the characters they save: a latin word cut in
    half ("do not bui…"), and a reminder that buries its decision behind a
    bracketed rationale the reader has to skip past.
    """

    def setUp(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import daily_brief

        self.db = daily_brief

    def test_latin_text_is_cut_at_a_word_boundary(self):
        text = "burn on a trip that already exists; do not build a trip for it"
        got = self.db._truncate(text, 40)
        self.assertTrue(got.endswith("…"), got)
        self.assertNotIn("bui…", got)
        self.assertFalse(got[:-1].rstrip().endswith(" "), got)

    def test_cjk_still_uses_the_full_budget(self):
        """CJK has no word boundary; retreating would throw away the line."""
        text = "落到已有行程别为它造行程再多写一些字撑满这一行的预算"
        got = self.db._truncate(text, 12)
        self.assertEqual(len(got), 12)

    def test_short_text_is_untouched(self):
        self.assertEqual(self.db._truncate("Example Hotel 2025 免房券", 96), "Example Hotel 2025 免房券")

    def test_a_bracketed_rationale_is_dropped_but_the_decision_survives(self):
        raw = (
            "演唱会: Some Artist · Some Hall "
            "[反画像 · 用一段很长的理由说明为什么这场值得测试跨类型兴趣] "
            "距离演出 14 天，尚未购票；要买票吗？"
        )
        got = self.db._format_reminder(raw)
        self.assertNotIn("反画像", got)
        self.assertIn("要买票吗", got)
        self.assertIn("Some Artist", got)

    def test_a_short_bracketed_label_is_kept(self):
        raw = "演唱会: Some Artist [已购票] 明晚"
        self.assertIn("[已购票]", self.db._format_reminder(raw))


class ReconciliationTests(unittest.TestCase):
    """A perk redeemed mid-week must not sit on the morning screen until the
    weekly index refresh: a newer note naming the row's due date flags it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, vault: Path) -> dict:
        proc = subprocess.run(
            [sys.executable, "scripts/daily_brief.py", "--json", "--today", TODAY, "--skip-cues", "--cap", "40"],
            cwd=REPO_ROOT,
            env={**os.environ, "OV": str(vault)},
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _item(self, brief: dict, label_start: str) -> dict:
        for group in brief["groups"]:
            for item in group["items"]:
                if item.get("label", "").startswith(label_start):
                    return item
        raise AssertionError(f"no item starting {label_start!r}")

    def _note(self, vault: Path, text: str, *, newer: bool, at: datetime | None = None) -> Path:
        path = vault / "travel" / "trips" / "example-city.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if newer or at:
            stamp = (at or datetime(2099, 2, 1, 12, 0)).timestamp()
            os.utime(path, (stamp, stamp))
        return path

    def test_skip_prefixes_follow_the_path_registry(self):
        from unittest import mock

        ov = Path(self.tmp.name)
        with mock.patch("_paths.tier_segments", return_value={"archive": "attic", "wiki": "kb", "papers": "papers"}), \
             mock.patch("_paths.wiki_dirs", return_value=[ov / "kb", ov / "kb-zh", Path("/elsewhere/kb")]):
            prefixes = db._reconcile_skip_prefixes(ov)
        self.assertIn("attic", prefixes)
        self.assertNotIn("archive", prefixes)
        self.assertIn("kb-zh", prefixes)
        self.assertIn("cache", prefixes)
        self.assertNotIn("/elsewhere/kb", prefixes)
        # With the registry unreadable the literal names still exclude.
        with mock.patch("_paths.tier_segments", side_effect=RuntimeError("no registry")):
            self.assertIn("archive", db._reconcile_skip_prefixes(ov))

    def test_a_note_under_an_excluded_tier_never_flags(self):
        vault = build_vault(Path(self.tmp.name))
        for sub in ("wiki", "archive", "inbox/digest", "cache"):
            path = vault / sub / "note.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Award night (exp 2099-03-15) 顺带在此兑现。\n", encoding="utf-8")
            stamp = datetime(2099, 2, 1, 12, 0).timestamp()
            os.utime(path, (stamp, stamp))
        self.assertNotIn("flag", self._item(self._run(vault), "Award night"))

    def test_same_day_notes_are_measured_against_the_refresh_moment(self):
        """The index says refreshed = 2099-01-30. When the file was written
        at noon that day, a note from 09:00 was already seen by the refresh
        and a note from 15:00 was not."""
        vault = build_vault(Path(self.tmp.name))
        index = vault / "_meta" / "deadlines.toml"
        noon = datetime(2099, 1, 30, 12, 0).timestamp()
        os.utime(index, (noon, noon))
        text = "Award night (exp 2099-03-15) 顺带在此兑现。\n"
        self._note(vault, text, newer=False, at=datetime(2099, 1, 30, 9, 0))
        self.assertNotIn("flag", self._item(self._run(vault), "Award night"))
        self._note(vault, text, newer=False, at=datetime(2099, 1, 30, 15, 0))
        brief = self._run(vault)
        self.assertEqual(self._item(brief, "Award night")["flag"], "待核")
        self.assertTrue(any("2099-01-30 刷新后" in w for w in brief["warnings"]), brief["warnings"])
        # Once the index file is touched on a later day (e.g. by `done`), the
        # cutoff falls back to the refresh date's midnight.
        later = datetime(2099, 2, 3, 8, 0).timestamp()
        os.utime(index, (later, later))
        self._note(vault, text, newer=False, at=datetime(2099, 1, 30, 9, 0))
        self.assertEqual(self._item(self._run(vault), "Award night")["flag"], "待核")

    def test_a_same_day_done_write_is_not_the_refresh_moment(self):
        """`done` rewrites the index; when that happens on the refresh date the
        file's mtime is the close, not the refresh, so the cutoff falls back
        to midnight and a note from earlier that day still flags."""
        vault = build_vault(Path(self.tmp.name))
        index = vault / "_meta" / "deadlines.toml"
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                'slug = "reversible-chore"', 'status = "done"\nresolved = 2099-01-30\nslug = "reversible-chore"'
            ),
            encoding="utf-8",
        )
        evening = datetime(2099, 1, 30, 20, 0).timestamp()
        os.utime(index, (evening, evening))
        text = "Award night (exp 2099-03-15) 顺带在此兑现。\n"
        self._note(vault, text, newer=False, at=datetime(2099, 1, 30, 9, 0))
        self.assertEqual(self._item(self._run(vault), "Award night")["flag"], "待核")

    def test_refreshed_at_is_the_cutoff_when_the_index_records_it(self):
        vault = build_vault(Path(self.tmp.name))
        index = vault / "_meta" / "deadlines.toml"
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "refreshed = 2099-01-30\n", "refreshed = 2099-01-30\nrefreshed_at = 2099-01-30T12:00:00\n"
            ).replace('slug = "reversible-chore"', 'status = "done"\nresolved = 2099-01-30\nslug = "reversible-chore"'),
            encoding="utf-8",
        )
        evening = datetime(2099, 1, 30, 20, 0).timestamp()
        os.utime(index, (evening, evening))
        text = "Award night (exp 2099-03-15) 顺带在此兑现。\n"
        self._note(vault, text, newer=False, at=datetime(2099, 1, 30, 9, 0))
        self.assertNotIn("flag", self._item(self._run(vault), "Award night"))
        self._note(vault, text, newer=False, at=datetime(2099, 1, 30, 15, 0))
        self.assertEqual(self._item(self._run(vault), "Award night")["flag"], "待核")

    def test_a_newer_note_naming_the_due_date_flags_the_row(self):
        vault = build_vault(Path(self.tmp.name))
        self._note(vault, "## 已锁定\n\nAward night (exp 2099-03-15) 顺带在此兑现。\n", newer=True)
        brief = self._run(vault)
        item = self._item(brief, "Award night")
        self.assertEqual(item["flag"], "待核")
        self.assertEqual(item["flag_source"], "travel/trips/example-city.md:3")
        self.assertTrue(any("needs-long-lead" in w and "deadlines.py done" in w for w in brief["warnings"]), brief["warnings"])

    def test_every_way_of_writing_the_day_counts(self):
        for text in (
            "Award night booked, was expiring 3/15.\n",
            "Award night booked, was expiring 03/15.\n",
            "Award night 03月15日 到期，已用掉。\n",
            "Award night 3月15日 到期，已用掉。\n",
            "Award night used, 2099/03/15 deadline gone.\n",
        ):
            with self.subTest(text=text):
                vault = build_vault(Path(self.tmp.name))
                self._note(vault, text, newer=True)
                self.assertEqual(self._item(self._run(vault), "Award night")["flag_source"], "travel/trips/example-city.md:1")
        self.assertEqual(
            db._date_forms("2099-03-15"),
            ["2099-03-15", "2099/03/15", "3/15", "3月15日", "03/15", "03月15日"],
        )
        # No duplicates when padding changes nothing.
        self.assertEqual(db._date_forms("2099-11-20"), ["2099-11-20", "2099/11/20", "11/20", "11月20日"])

    def test_label_words_match_regardless_of_case(self):
        for text in ("award night booked 3/15.\n", "AWARD NIGHT used, 2099-03-15.\n"):
            with self.subTest(text=text):
                vault = build_vault(Path(self.tmp.name))
                self._note(vault, text, newer=True)
                self.assertEqual(self._item(self._run(vault), "Award night")["flag"], "待核")
        self.assertEqual(db.label_tokens("Award Night 免房券 2025"), ["award", "night", "免房券"])

    def test_a_short_date_form_does_not_match_inside_a_longer_one(self):
        """`2/1` (the 02-01 row) must not match inside `02/15` or `12/10`."""
        for text in ("Document expires on 02/15.\n", "Document expires 12/10.\n", "Document expires 2/10.\n"):
            with self.subTest(text=text):
                vault = build_vault(Path(self.tmp.name))
                self._note(vault, text, newer=True)
                self.assertNotIn("flag", self._item(self._run(vault), "Document expires"))
        vault = build_vault(Path(self.tmp.name))
        self._note(vault, "Document expires 2/1, renewed.\n", newer=True)
        self.assertEqual(self._item(self._run(vault), "Document expires")["flag"], "待核")
        pattern = db._date_pattern("2099-02-01")
        self.assertIsNone(pattern.search("02/15"))
        self.assertIsNone(pattern.search("12/1"))
        self.assertIsNotNone(pattern.search("due 2/1."))
        self.assertIsNotNone(pattern.search("2月1日"))

    def test_an_older_note_or_a_date_without_the_label_does_not_flag(self):
        vault = build_vault(Path(self.tmp.name))
        self._note(vault, "Award night (exp 2099-03-15) 顺带在此兑现。\n", newer=False)
        self.assertNotIn("flag", self._item(self._run(vault), "Award night"))
        self._note(vault, "Something else happens on 2099-03-15.\n", newer=True)
        self.assertNotIn("flag", self._item(self._run(vault), "Award night"))

    def test_the_rows_own_source_never_flags_itself(self):
        vault = build_vault(Path(self.tmp.name))
        source = vault / "finance" / "example-tracker.md"
        source.write_text("Award night needs a booking, expires 2099-03-15\n" * 140, encoding="utf-8")
        stamp = datetime(2099, 2, 1, 12, 0).timestamp()
        os.utime(source, (stamp, stamp))
        brief = self._run(vault)
        self.assertNotIn("flag", self._item(brief, "Award night"))

    def test_items_carry_label_and_hint_apart_from_the_composed_text(self):
        brief = self._run(build_vault(Path(self.tmp.name)))
        for group in brief["groups"]:
            for item in group["items"]:
                self.assertIn("label", item, item)
                self.assertNotIn("d ·", item["label"])
                if "hint" in item:
                    self.assertTrue(item["text"].endswith(item["hint"]), item)
                    self.assertNotIn(item["hint"], item["label"])
        self.assertIn("[待核", db.text_view({**brief, "groups": [{"heading": "h", "items": [{"text": "x", "flag": "待核", "flag_source": "a.md:1"}]}]}))


if __name__ == "__main__":
    unittest.main()
