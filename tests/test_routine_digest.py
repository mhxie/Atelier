"""Tests for scripts/routine_digest.py.

Anchored on the shapes real scheduled routines emit, because each of these was a
bug found while building against a live vault:

  - filenames carry the date in three different positions
  - collector reports pack N findings into one file, each with its own
    frontmatter block and `slug`; a file-level headline threw most of it away
  - the first heading of such a report is collection bookkeeping
    ("Collection status"), which makes a useless headline but a fine excerpt
  - `---` is both a frontmatter fence and a horizontal rule
  - excerpts are escaped at render time, so markdown markup must be stripped
    at extraction time or the asterisks print
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _paths  # noqa: E402
import routine_digest as rd  # noqa: E402

def _set_vault(vault: Path) -> str | None:
    """Point $OV at a fixture vault, returning the previous value.

    Restoring it in tearDown matters: these modules import scripts in-process,
    and a leaked $OV pointing at a deleted temp directory would corrupt any
    later test in the same run.
    """
    previous = os.environ.get("OV")
    os.environ["OV"] = str(vault)
    _paths.reset()
    return previous


def _restore_vault(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("OV", None)
    else:
        os.environ["OV"] = previous
    _paths.reset()


WATCH_TOML = """
[coordination]
backend = "owner"

[[routine]]
name = "feed-digest"
label = "daily feed digest"
output_dir = "inbox/feed"
file_pattern = "*-feed.md"

[[routine]]
name = "policy-monitor"
label = "policy monitor"
output_dir = "finance/signals"
file_pattern = "*-monitor.md"

[[routine]]
name = "role-scan"
label = "role scan"
output_dir = "career/scans"
file_pattern = "role_scan_*.md"

[[routine]]
name = "autoevo-nightly"
label = "nightly decay sweep"
output_dir = "agent-findings"
file_pattern = "autoevo-applied-*.md"

[[routine]]
name = "digest-writer"
label = "digest writer"
output_dir = "inbox/digest"
file_pattern = "*-digest.html"
digest = { include = false }
"""

TECH_DIGEST = """---
date: 2099-01-30
type: feed
item_count: 2
---

# Daily Feed Digest — 2099-01-30

## Items

1. [First item title](https://example.com/one)
   A blurb about the first item. **Category · familiar**
2. [Second item title](https://example.com/two)
   Another blurb.
"""

# Two findings in one file, frontmatter blocks in the middle, plus a real
# horizontal rule that must not be mistaken for a fence.
SIGNAL_REPORT = """## Collection status

- Window checked: 2099-01-18 through 2099-01-25.
- One news source was blocked by robots.txt.

---
date: 2099-01-25
slug: rate-decision-signal
source_url: https://example.com/minutes
source_type: regulatory
signal_type: [G, D]
---

## Facts

- The rate corridor was held unchanged on a split vote.

## Why This Matters

The record adds a **valuation** risk signal for the [capex
complex](https://example.com/x): a tighter path raises discount rates.

---
date: 2099-01-25
slug: trade-order-signal
source_url: https://example.org/notice
source_type: regulatory
---

## Facts

- Final trade orders were published.

## Why This Matters

Input costs rise downstream.
"""

ROLE_SCAN = """## Role Scan Run 2099-01-25 (3-day window)

No qualifying inbound this window.
"""

UPDATE_CONFIG = """
[[source]]
name = "status-ledger"
label = "Status ledger"
path = "personal/status-tracker.md"
section = "Monthly audit ledger"
date_column = "Checked"
display_columns = ["Period", "Cutoff", "Eligible", "Action", "Sources"]
since = 2099-01-01
"""

UPDATE_LEDGER = """# Status Tracker

## Monthly audit ledger

| Period | Checked | Cutoff | Eligible | Action | Sources |
|---|---|---|---|---|---|
| 2099-01 | 2099-01-30 | 2098-06-01 | No | Keep monitoring | [Primary](https://example.com/status) |
| 2099-02 | 2099-01-30 | 2098-07-01 | No | Keep monitoring | [Primary](https://example.com/status-2) |
"""


def build_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "_meta").mkdir(parents=True)
    (vault / "_meta" / "routine_watch.toml").write_text(WATCH_TOML, encoding="utf-8")
    (vault / "_meta" / "digest_updates.toml").write_text(UPDATE_CONFIG, encoding="utf-8")

    (vault / "personal").mkdir(parents=True)
    (vault / "personal" / "status-tracker.md").write_text(UPDATE_LEDGER, encoding="utf-8")

    (vault / "inbox" / "feed").mkdir(parents=True)
    for day in ("24", "30"):
        (vault / "inbox" / "feed" / f"2099-01-{day}-feed.md").write_text(
            TECH_DIGEST.replace("2099-01-30", f"2099-01-{day}"), encoding="utf-8"
        )

    signals = vault / "finance" / "signals"
    signals.mkdir(parents=True)
    (signals / "2099-01-25-monitor.md").write_text(SIGNAL_REPORT, encoding="utf-8")

    runs = vault / "career" / "scans"
    runs.mkdir(parents=True)
    (runs / "role_scan_2099-01-28.md").write_text(ROLE_SCAN, encoding="utf-8")

    findings = vault / "agent-findings"
    findings.mkdir(parents=True)
    (findings / "autoevo-applied-2099-01-30.md").write_text("# Applied\n\n- ...\n", encoding="utf-8")
    return vault


class ExtractionTests(unittest.TestCase):
    def test_file_date_from_any_position(self):
        self.assertEqual(
            rd.file_date(Path("2099-01-30-feed.md"))[0].isoformat(), "2099-01-30"
        )
        self.assertEqual(
            rd.file_date(Path("role_scan_2099-01-28.md"))[0].isoformat(), "2099-01-28"
        )
        self.assertEqual(
            rd.file_date(Path("weekly-sweep-2099-01-22.md"))[0].isoformat(), "2099-01-22"
        )

    def test_file_date_falls_back_to_mtime_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no-date-here.md"
            path.write_text("x", encoding="utf-8")
            _, source = rd.file_date(path)
            self.assertEqual(source, "mtime")

    def test_split_units_finds_mid_file_blocks(self):
        units = rd.split_units(SIGNAL_REPORT)
        self.assertEqual(
            [meta["slug"] for meta, _ in units],
            ["rate-decision-signal", "trade-order-signal"],
        )

    def test_split_units_ignores_metadata_block_without_slug(self):
        self.assertEqual(rd.split_units(TECH_DIGEST), [])

    def test_horizontal_rule_is_not_a_frontmatter_fence(self):
        text = "para one\n\n---\n\npara two\n\n---\n\npara three\n"
        self.assertEqual(rd.split_units(text), [])

    def test_unit_body_stops_at_the_next_block(self):
        units = rd.split_units(SIGNAL_REPORT)
        first_body = units[0][1]
        self.assertIn("split vote", first_body)
        self.assertNotIn("trade orders", first_body)

    def test_headline_prefers_h1(self):
        meta, body = rd.parse_frontmatter(TECH_DIGEST)
        self.assertEqual(
            rd.extract_headline(meta, body, []), "Daily Feed Digest — 2099-01-30"
        )

    def test_headline_of_multi_signal_file_names_its_slugs(self):
        units = rd.split_units(SIGNAL_REPORT)
        headline = rd.extract_headline({}, SIGNAL_REPORT, units)
        self.assertTrue(headline.startswith("2 signals:"), headline)
        self.assertIn("rate decision signal", headline)
        self.assertNotIn("Collection status", headline)

    def test_headline_skips_bookkeeping_headings(self):
        body = "## Collection status\n\n- coverage note\n\n## Real Finding\n\ntext\n"
        self.assertEqual(rd.extract_headline({}, body, []), "Real Finding")

    def test_excerpt_prefers_the_payload_section(self):
        units = rd.split_units(SIGNAL_REPORT)
        excerpt = rd.extract_excerpt(units[0][1], 400)
        self.assertIn("valuation", excerpt)
        self.assertNotIn("split vote", excerpt)

    def test_excerpt_strips_markdown_markup(self):
        units = rd.split_units(SIGNAL_REPORT)
        excerpt = rd.extract_excerpt(units[0][1], 400)
        self.assertNotIn("**", excerpt)
        self.assertNotIn("](", excerpt)
        self.assertIn("capex complex", excerpt)

    def test_excerpt_truncates_at_a_sentence_boundary(self):
        text = "## Why This Matters\n\n" + ("Sentence one is here. " * 20)
        excerpt = rd.extract_excerpt(text, 100)
        self.assertLessEqual(len(excerpt), 110)
        self.assertTrue(excerpt.endswith("…"))

    def test_items_come_from_lists_not_prose(self):
        items = rd.extract_items(TECH_DIGEST, 15)
        self.assertEqual([i["url"] for i in items], ["https://example.com/one", "https://example.com/two"])
        prose = "See [a citation](https://example.com/cited) in this paragraph.\n"
        self.assertEqual(rd.extract_items(prose, 15), [])

    def test_items_respect_the_cap(self):
        body = "\n".join(f"{n}. [t{n}](https://example.com/{n})" for n in range(20))
        self.assertEqual(len(rd.extract_items(body, 5)), 5)


class WindowTests(unittest.TestCase):
    def test_weekly_window_is_seven_inclusive_days(self):
        start, end, span = rd.resolve_window("weekly", days=None, since=None, until="2099-01-30")
        self.assertEqual((start.isoformat(), end.isoformat(), span), ("2099-01-24", "2099-01-30", 7))

    def test_daily_window_is_one_day(self):
        start, end, span = rd.resolve_window("daily", days=None, since=None, until="2099-01-30")
        self.assertEqual((start.isoformat(), end.isoformat(), span), ("2099-01-30", "2099-01-30", 1))

    def test_effective_date_rolls_back_before_three_am(self):
        from datetime import datetime

        self.assertEqual(
            rd.effective_date(datetime(2099, 1, 31, 1, 30)).isoformat(), "2099-01-30"
        )
        self.assertEqual(
            rd.effective_date(datetime(2099, 1, 31, 9, 0)).isoformat(), "2099-01-31"
        )

    def test_since_after_until_is_rejected(self):
        with self.assertRaises(SystemExit):
            rd.resolve_window("weekly", days=None, since="2099-01-30", until="2099-01-24")


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))
        self._prev_ov = _set_vault(self.vault)

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev_ov)

    def test_window_selects_only_in_range_files(self):
        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        names = sorted(s["name"] for _, s in rd.iter_sources(manifest))
        self.assertEqual(
            names,
            [
                "2099-01-24-feed.md",
                "2099-01-25-monitor.md",
                "2099-01-30-feed.md",
                "role_scan_2099-01-28.md",
            ],
        )

    def test_maintenance_routine_excluded_by_default(self):
        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.assertIn("nightly decay sweep", manifest["skipped_routines"])
        self.assertNotIn(
            "autoevo-applied-2099-01-30.md",
            [s["name"] for _, s in rd.iter_sources(manifest)],
        )

    def test_maintenance_routine_included_on_request(self):
        manifest = rd.collect(
            self.vault, mode="weekly", until="2099-01-30", include_maintenance=True
        )
        self.assertIn(
            "autoevo-applied-2099-01-30.md",
            [s["name"] for _, s in rd.iter_sources(manifest)],
        )

    def test_daily_mode_narrows_to_one_day(self):
        manifest = rd.collect(self.vault, mode="daily", until="2099-01-30")
        self.assertEqual(
            [s["name"] for _, s in rd.iter_sources(manifest)], ["2099-01-30-feed.md"]
        )

    def test_lanes_derive_from_output_dir_when_undeclared(self):
        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.assertEqual(
            [lane["lane"] for lane in manifest["lanes"]], ["Tech feed", "Finance", "Career"]
        )

    def test_declared_lane_overrides_the_default(self):
        watch = self.vault / "_meta" / "routine_watch.toml"
        watch.write_text(
            WATCH_TOML.replace(
                'file_pattern = "*-feed.md"',
                'file_pattern = "*-feed.md"\ndigest = { lane = "Research" }',
            ),
            encoding="utf-8",
        )
        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.assertIn("Research", [lane["lane"] for lane in manifest["lanes"]])

    def test_declared_exclusion_drops_a_routine(self):
        watch = self.vault / "_meta" / "routine_watch.toml"
        watch.write_text(
            WATCH_TOML.replace(
                'file_pattern = "role_scan_*.md"',
                'file_pattern = "role_scan_*.md"\ndigest = { include = false }',
            ),
            encoding="utf-8",
        )
        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.assertNotIn("Career", [lane["lane"] for lane in manifest["lanes"]])
        self.assertIn("role scan", manifest["skipped_routines"])

    def test_unacked_mode_ignores_the_window(self):
        (self.vault / "_meta" / "routine_acks.json").write_text(
            json.dumps({"inbox/feed": "2099-01-24-feed.md"}), encoding="utf-8"
        )
        manifest = rd.collect(self.vault, mode="weekly", until="2098-01-01", unacked=True)
        names = sorted(s["name"] for _, s in rd.iter_sources(manifest))
        self.assertEqual(
            names,
            [
                "2099-01-25-monitor.md",
                "2099-01-30-feed.md",
                "role_scan_2099-01-28.md",
            ],
        )

    def test_multi_signal_file_carries_units_not_a_file_excerpt(self):
        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        source = next(
            s for _, s in rd.iter_sources(manifest) if s["name"].endswith("monitor.md")
        )
        self.assertEqual(len(source["units"]), 2)
        self.assertNotIn("excerpt", source)
        self.assertEqual(
            source["units"][0]["source_url"], "https://example.com/minutes"
        )
        self.assertIn("valuation", source["units"][0]["excerpt"])

    def test_ack_targets_track_the_newest_file_per_directory(self):
        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.assertEqual(manifest["acks"]["inbox/feed"], "2099-01-30-feed.md")

    def test_daily_updates_use_a_delivery_cursor_not_the_date_window(self):
        # The row was checked yesterday, after that morning's report. It must
        # land in today's daily artifact and then disappear from the next run.
        manifest = rd.collect(self.vault, mode="daily", until="2099-01-31")
        self.assertEqual(manifest["counts"]["files"], 0)
        self.assertEqual(len(manifest["updates"]), 2)
        self.assertEqual(manifest["updates"][0]["values"]["Period"], "2099-01")
        self.assertEqual(manifest["updates"][1]["values"]["Period"], "2099-02")

        rd.write(self.vault, rd.render(manifest), manifest, routine_name="digest-writer")
        state = json.loads(
            (self.vault / rd.DIGEST_UPDATES_STATE).read_text(encoding="utf-8")
        )
        self.assertEqual(state["daily"]["status-ledger"], manifest["updates"][-1]["id"])
        replay = rd.collect(self.vault, mode="daily", until="2099-02-01")
        self.assertEqual(replay["updates"], [])

    def test_weekly_update_window_is_independent_of_daily_delivery(self):
        daily = rd.collect(self.vault, mode="daily", until="2099-01-31")
        rd.write(self.vault, rd.render(daily), daily, routine_name="digest-writer")

        weekly = rd.collect(self.vault, mode="weekly", until="2099-01-31")
        self.assertEqual(len(weekly["updates"]), 2)

    def test_backdated_daily_does_not_pull_a_future_update(self):
        manifest = rd.collect(self.vault, mode="daily", until="2099-01-29")
        self.assertEqual(manifest["updates"], [])

    def test_empty_window_is_not_an_error(self):
        manifest = rd.collect(self.vault, mode="weekly", until="2020-01-01")
        self.assertEqual(manifest["counts"]["files"], 0)
        self.assertEqual(manifest["lanes"], [])

    def test_max_files_truncates_and_flags_it(self):
        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30", max_files=2)
        self.assertTrue(manifest["truncated"])
        self.assertEqual(manifest["counts"]["files"], 2)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))
        self._prev_ov = _set_vault(self.vault)
        self.manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev_ov)

    def test_title_shapes(self):
        self.assertEqual(rd.digest_title(self.manifest), "Atelier Weekly — 2099-01-24 → 2099-01-30")
        daily = rd.collect(self.vault, mode="daily", until="2099-01-30")
        self.assertEqual(rd.digest_title(daily), "Atelier Daily — 2099-01-30")
        backlog = rd.collect(self.vault, mode="weekly", until="2099-01-30", unacked=True)
        self.assertIn("backlog", rd.digest_title(backlog))

    def test_brief_renders_above_the_overview(self):
        """The action surface is the first screen, ahead of the intel overview."""
        brief = {
            "schema": 1,
            "date": "2099-01-30",
            "groups": [
                {
                    "tier": 1,
                    "kind": "closing_now",
                    "heading": "今天/明天关窗 1 件",
                    "folded": False,
                    "items": [{"text": "Hotel credit 明天", "source": "finance/example-tracker.md:107"}],
                }
            ],
            "warnings": ["deadline index stale 12d"],
        }
        overview = {"schema": 1, "sections": [{"title": "情报", "bullets": [{"text": "x"}]}]}
        document = rd.render(self.manifest, overview, brief)
        self.assertIn("今天/明天关窗 1 件", document)
        self.assertIn("Hotel credit 明天", document)
        self.assertIn("deadline index stale 12d", document)
        self.assertLess(document.index("今天/明天关窗"), document.index("情报"))
        self.assertLess(document.index("情报"), document.index("Source index"))

    def test_brief_is_optional(self):
        """Without a brief there is no first screen, so no card is drawn."""
        document = rd.render(self.manifest)
        self.assertNotIn(rd._S_CARD, document)
        self.assertIn("Source index", document)

    def test_configured_updates_render_before_the_overview(self):
        overview = {"schema": 1, "sections": [{"title": "情报", "bullets": [{"text": "x"}]}]}
        document = rd.render(self.manifest, overview)
        self.assertIn("状态更新", document)
        self.assertIn("Status ledger", document)
        self.assertIn("2098-06-01", document)
        self.assertIn('href="https://example.com/status"', document)
        self.assertLess(document.index("状态更新"), document.index("情报"))

    def test_folded_brief_group_renders_heading_only(self):
        brief = {
            "schema": 1,
            "date": "2099-01-30",
            "groups": [
                {
                    "tier": 3,
                    "kind": "recurring",
                    "heading": "recurring: 9 条逾期",
                    "folded": True,
                    "items": [],
                }
            ],
            "warnings": [],
        }
        # Assert on the first screen itself rather than by slicing the whole
        # document: string surgery on rendered HTML breaks whenever unrelated
        # copy happens to reuse a word.
        card = rd._render_brief(brief)
        self.assertIn("recurring: 9 条逾期", card)
        self.assertNotIn("<li", card)
        self.assertIn(card, rd.render(self.manifest, None, brief))

    def test_unknown_brief_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                rd.load_brief(path)

    def test_brief_text_is_escaped(self):
        brief = {
            "schema": 1,
            "date": "2099-01-30",
            "groups": [
                {
                    "tier": 1,
                    "kind": "closing_now",
                    "heading": "<script>h</script>",
                    "folded": False,
                    "items": [{"text": "<img onerror=x>"}],
                }
            ],
            "warnings": ["<b>w</b>"],
        }
        document = rd.render(self.manifest, None, brief)
        self.assertNotIn("<script>", document)
        self.assertNotIn("<img", document)
        self.assertIn("&lt;script&gt;", document)

    def test_render_without_overview_still_produces_the_index(self):
        document = rd.render(self.manifest)
        self.assertIn("No overview supplied", document)
        self.assertIn("Source index", document)
        self.assertIn("daily feed digest", document)

    def test_render_includes_item_links_and_provenance_path(self):
        document = rd.render(self.manifest)
        self.assertIn('href="https://example.com/one"', document)
        self.assertRegex(document, r'<code[^>]*>inbox/feed/2099-01-30-feed\.md</code>')

    def _overview_with_source(self, source_path: str) -> dict:
        return {
            "schema": 1,
            "headline": "One line",
            "sections": [
                {
                    "title": "这周",
                    "bullets": [
                        {
                            "text": "A **bold** claim with a [link](https://example.com/z)",
                            "sources": [source_path],
                        }
                    ],
                }
            ],
        }

    def test_overview_bullets_render_inline_markup_and_real_links(self):
        source_path = "inbox/feed/2099-01-30-feed.md"
        document = rd.render(self.manifest, self._overview_with_source(source_path))
        self.assertIn("<strong>bold</strong>", document)
        self.assertIn('href="https://example.com/z"', document)
        # The index entry keeps its id for the browser case.
        self.assertIn(f'id="{rd.source_anchor(source_path)}"', document)

    def test_source_references_are_not_in_document_anchors(self):
        """Gmail rewrites `#anchor` hrefs, so a bullet must not pretend to link.

        The document's primary destination is an email client. A source
        reference that looks clickable and navigates nowhere is worse than a
        plain label, so provenance renders as a label and the anchor link is
        gone on purpose.
        """
        source_path = "inbox/feed/2099-01-30-feed.md"
        document = rd.render(self.manifest, self._overview_with_source(source_path))
        self.assertNotIn(f'href="#{rd.source_anchor(source_path)}"', document)
        self.assertRegex(document, r'<code[^>]*>2099-01-30-feed\.md</code>')

    def test_a_source_outside_the_manifest_is_marked_unmatched(self):
        document = rd.render(
            self.manifest, self._overview_with_source("finance/invented.md")
        )
        self.assertIn("unmatched", document)
        self.assertNotIn("<code>invented.md</code>", document)

    def test_overview_html_is_inert(self):
        overview = {
            "schema": 1,
            "sections": [
                {"title": "T", "bullets": [{"text": "<script>alert(1)</script> and <b>x</b>"}]}
            ],
        }
        document = rd.render(self.manifest, overview)
        self.assertNotIn("<script>", document)
        self.assertIn("&lt;script&gt;", document)

    def test_unknown_overview_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "o.json"
            path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                rd.load_overview(path)

    def test_excluded_routines_are_disclosed_in_the_document(self):
        document = rd.render(self.manifest)
        self.assertIn("Excluded from this digest", document)
        self.assertIn("nightly decay sweep", document)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))
        self._prev_ov = _set_vault(self.vault)
        self.manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev_ov)

    def test_artifact_lands_in_the_routines_declared_directory(self):
        code = rd.write(
            self.vault, "<h1>x</h1>", self.manifest, routine_name="digest-writer"
        )
        self.assertEqual(code, 0)
        written = self.vault / "inbox" / "digest" / "2099-01-30-weekly-digest.html"
        self.assertTrue(written.is_file())
        self.assertEqual(written.read_text(encoding="utf-8"), "<h1>x</h1>")

    def test_a_routine_that_would_ingest_itself_is_refused(self):
        # `feed-digest` has no `include = false`, so writing the digest into its
        # directory would feed tomorrow's run its own output.
        with self.assertRaises(SystemExit) as caught:
            rd.write(
                self.vault, "<h1>x</h1>", self.manifest, routine_name="feed-digest"
            )
        self.assertIn("not excluded", str(caught.exception))

    def test_unknown_routine_is_refused(self):
        with self.assertRaises(SystemExit):
            rd.write(self.vault, "<h1>x</h1>", self.manifest, routine_name="nope")

    def test_routine_is_inferred_from_the_only_explicit_exclusion(self):
        # The procedure cannot name the routine (the registry is private and
        # the name is not exported into the sandbox), so the one row with an
        # explicit `include = false` is the digest's own. autoevo-nightly is
        # excluded by default and must not count.
        code = rd.write(self.vault, "<h1>x</h1>", self.manifest)
        self.assertEqual(code, 0)
        written = self.vault / "inbox" / "digest" / "2099-01-30-weekly-digest.html"
        self.assertTrue(written.is_file())

    def test_inference_refuses_ambiguity_and_absence(self):
        registry = self.vault / "_meta" / "routine_watch.toml"
        original = registry.read_text(encoding="utf-8")
        registry.write_text(
            original.replace(
                'file_pattern = "*-feed.md"', 'file_pattern = "*-feed.md"\ndigest = { include = false }'
            ),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as caught:
            rd.write(self.vault, "<h1>x</h1>", self.manifest)
        self.assertIn("pass --routine", str(caught.exception))
        self.assertIn("feed-digest", str(caught.exception))
        registry.write_text(
            original.replace("digest = { include = false }", ""), encoding="utf-8"
        )
        with self.assertRaises(SystemExit) as caught:
            rd.write(self.vault, "<h1>x</h1>", self.manifest)
        self.assertIn("include = false", str(caught.exception))

    def test_dry_run_reports_the_path_without_writing(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = rd.write(
                self.vault,
                "<h1>x</h1>",
                self.manifest,
                routine_name="digest-writer",
                dry_run=True,
            )
        self.assertEqual(code, 0)
        self.assertIn("would write", buffer.getvalue())
        self.assertFalse((self.vault / "inbox" / "digest").exists())

    def test_gmail_clip_size_warns_but_still_writes(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = rd.write(
                self.vault,
                "x" * (rd.GMAIL_CLIP_BYTES + 1),
                self.manifest,
                routine_name="digest-writer",
            )
        self.assertEqual(code, 0)
        self.assertIn("Gmail clips", buffer.getvalue())
        self.assertTrue(
            (self.vault / "inbox" / "digest" / "2099-01-30-weekly-digest.html").is_file()
        )


class AckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))
        self._prev_ov = _set_vault(self.vault)
        self.manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.ack_path = self.vault / "_meta" / "routine_acks.json"

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev_ov)

    def test_ack_writes_the_newest_name_per_directory(self):
        rd.ack(self.vault, self.manifest)
        data = json.loads(self.ack_path.read_text(encoding="utf-8"))
        self.assertEqual(data["inbox/feed"], "2099-01-30-feed.md")
        self.assertEqual(data["career/scans"], "role_scan_2099-01-28.md")

    def test_ack_never_moves_backwards(self):
        self.ack_path.write_text(
            json.dumps({"inbox/feed": "2099-02-99-feed.md"}), encoding="utf-8"
        )
        rd.ack(self.vault, self.manifest)
        data = json.loads(self.ack_path.read_text(encoding="utf-8"))
        self.assertEqual(data["inbox/feed"], "2099-02-99-feed.md")

    def test_ack_preserves_unrelated_directories(self):
        self.ack_path.write_text(json.dumps({"some/other/dir": "keep-me.md"}), encoding="utf-8")
        rd.ack(self.vault, self.manifest)
        data = json.loads(self.ack_path.read_text(encoding="utf-8"))
        self.assertEqual(data["some/other/dir"], "keep-me.md")

    def test_ack_dry_run_does_not_write(self):
        rd.ack(self.vault, self.manifest, dry_run=True)
        self.assertFalse(self.ack_path.exists())

    def test_acking_a_full_digest_clears_the_cue(self):
        """The point of ack: cues.py goes quiet for everything the digest covered."""
        import cues

        manifest = rd.collect(
            self.vault, mode="weekly", until="2099-01-30", unacked=True, include_maintenance=True
        )
        rd.ack(self.vault, manifest)
        cue, _debug = cues.check_routine_outputs(self.vault, rd.effective_date())
        self.assertIsNone(cue, cue.message if cue else "")

    def test_ack_leaves_the_cue_up_for_routines_the_digest_excluded(self):
        """An excluded routine is not read, so its review debt must survive.

        The digest drops harness-maintenance output by default; acking must not
        silently mark it reviewed, or /autoevo-review loses its only nag.
        """
        import cues

        manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30", unacked=True)
        rd.ack(self.vault, manifest)
        cue, _debug = cues.check_routine_outputs(self.vault, rd.effective_date())
        self.assertIsNotNone(cue)
        self.assertIn("nightly decay sweep", cue.message)
        self.assertNotIn("daily feed digest", cue.message)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/routine_digest.py", *args],
            cwd=REPO_ROOT,
            env={**os.environ, "OV": str(self.vault)},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_end_to_end_collect_render_write(self):
        manifest_path = Path(self.tmp.name) / "m.json"
        html_path = Path(self.tmp.name) / "d.html"

        proc = self._run(
            "collect", "--mode", "weekly", "--until", "2099-01-30", "--json",
            "--out", str(manifest_path),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(manifest_path.read_text())["counts"]["files"], 4)

        proc = self._run("render", "--manifest", str(manifest_path), "--out", str(html_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Source index", html_path.read_text(encoding="utf-8"))

        proc = self._run(
            "write", "--manifest", str(manifest_path), "--routine", "digest-writer"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = self.vault / "inbox" / "digest" / "2099-01-30-weekly-digest.html"
        self.assertTrue(written.is_file())
        self.assertIn("Source index", written.read_text(encoding="utf-8"))

    def test_write_refuses_an_empty_window(self):
        manifest_path = Path(self.tmp.name) / "empty.json"
        self._run(
            "collect", "--until", "2020-01-01", "--json", "--out", str(manifest_path)
        )
        proc = self._run(
            "write", "--manifest", str(manifest_path), "--routine", "digest-writer"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("empty window", proc.stdout)
        self.assertFalse((self.vault / "inbox" / "digest").exists())

    def test_update_only_daily_manifest_still_writes(self):
        manifest_path = Path(self.tmp.name) / "updates.json"
        proc = self._run(
            "collect", "--mode", "daily", "--until", "2099-01-31", "--json",
            "--out", str(manifest_path),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"]["files"], 0)
        self.assertEqual(manifest["counts"]["updates"], 2)

        proc = self._run(
            "write", "--manifest", str(manifest_path), "--routine", "digest-writer"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = self.vault / "inbox" / "digest" / "2099-01-31-daily-digest.html"
        self.assertTrue(written.is_file())
        self.assertIn("Status ledger", written.read_text(encoding="utf-8"))

    def test_write_without_routine_uses_the_registry_exclusion(self):
        manifest_path = Path(self.tmp.name) / "m.json"
        self._run(
            "collect", "--until", "2099-01-30", "--json", "--out", str(manifest_path)
        )
        proc = self._run("write", "--manifest", str(manifest_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("inbox/digest/", proc.stdout)
        self.assertTrue(list((self.vault / "inbox" / "digest").glob("*-weekly-digest.html")))

    def test_text_report_is_the_default_output(self):
        proc = self._run("collect", "--until", "2099-01-30")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Tech feed", proc.stdout)
        self.assertNotIn('"schema"', proc.stdout)

    def test_missing_registry_fails_loudly(self):
        (self.vault / "_meta" / "routine_watch.toml").unlink()
        proc = self._run("collect")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("routine registry missing", proc.stderr)


if __name__ == "__main__":
    unittest.main()


class MailTests(unittest.TestCase):
    """Delivery is deterministic, and the recipient is not reachable from a prompt.

    Model-sent delivery is not merely riskier here, it does not work: the Codex
    Gmail plugin marks send_email as requiring approval and unattended routines
    run under approval_policy = "never". So the send is a script, the recipient
    comes from private config, and the body is the artifact verbatim.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "_meta").mkdir(parents=True)
        self._prev = _set_vault(self.vault)

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev)

    def _config(self, body: str) -> None:
        (self.vault / "_meta" / "mail.toml").write_text(body, encoding="utf-8")

    def test_a_missing_config_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            rd.load_mail_config(self.vault)
        self.assertIn("mail config missing", str(caught.exception))

    def test_an_incomplete_config_names_what_is_missing(self):
        self._config('[smtp]\nhost = "smtp.example.com"\n')
        with self.assertRaises(SystemExit) as caught:
            rd.load_mail_config(self.vault)
        self.assertIn("username", str(caught.exception))

    def test_a_config_with_no_password_source_is_refused(self):
        self._config('[smtp]\nhost = "h"\nusername = "a@b.c"\n')
        with self.assertRaises(SystemExit) as caught:
            rd.load_mail_config(self.vault)
        self.assertIn("keychain_service", str(caught.exception))
        self.assertIn("password_file", str(caught.exception))

    def test_either_password_source_alone_is_enough(self):
        self._config('[smtp]\nhost = "h"\nusername = "a@b.c"\npassword_file = "/x"\n')
        self.assertEqual(rd.load_mail_config(self.vault)["password_file"], "/x")

    def test_a_complete_config_loads(self):
        self._config(
            '[smtp]\nhost = "smtp.example.com"\nport = 587\n'
            'username = "someone@example.com"\nkeychain_service = "svc"\n'
        )
        smtp = rd.load_mail_config(self.vault)
        self.assertEqual(smtp["username"], "someone@example.com")

    def test_the_message_is_addressed_to_the_configured_account_only(self):
        message = rd.build_message(
            "<h1>x</h1>", "Subject", "someone@example.com", "someone@example.com"
        )
        self.assertEqual(message["To"], "someone@example.com")
        self.assertIsNone(message["Cc"])
        self.assertIsNone(message["Bcc"])

    def test_the_html_part_carries_the_artifact_verbatim(self):
        body = "<h1>Atelier Daily</h1><p>unique-marker-9f3</p>"
        message = rd.build_message(body, "S", "a@b.c", "a@b.c")
        html = [
            part for part in message.walk()
            if part.get_content_type() == "text/html"
        ][0]
        self.assertIn("unique-marker-9f3", html.get_content())

    def test_dry_run_sends_nothing_and_names_the_destination(self):
        import contextlib
        import io

        self._config(
            '[smtp]\nhost = "smtp.example.com"\nport = 587\n'
            'username = "someone@example.com"\nkeychain_service = "svc"\n'
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = rd.mail(self.vault, "<h1>x</h1>", "Subject", dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("someone@example.com", buffer.getvalue())
        self.assertIn("smtp.example.com:587", buffer.getvalue())

    def test_the_recipient_cannot_be_overridden_from_the_command_line(self):
        """There is no --to. Adding one would defeat the whole arrangement."""
        source = (REPO_ROOT / "scripts" / "routine_digest.py").read_text(encoding="utf-8")
        self.assertNotIn('"--to"', source)
        self.assertNotIn('"--recipient"', source)


class SmtpPasswordTests(unittest.TestCase):
    """The credential must never hang an unattended job, and never live in $OV.

    Inside the routine sandbox the keychain read blocks on an interaction prompt
    that no one will answer, which is worse than failing outright. So the read is
    hard-bounded and a file fallback exists for exactly that case.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, content: str, mode: int = 0o600) -> Path:
        path = self.dir / "secret"
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        return path

    def test_a_world_readable_password_file_is_refused(self):
        path = self._file("hunter2hunter2", mode=0o644)
        with self.assertRaises(SystemExit) as caught:
            rd.smtp_password({"username": "a@b.c", "password_file": str(path)})
        self.assertIn("0600", str(caught.exception))

    def test_a_protected_password_file_is_read(self):
        path = self._file("abcd efgh ijkl mnop")
        got = rd.smtp_password({"username": "a@b.c", "password_file": str(path)})
        self.assertEqual(got, "abcdefghijklmnop", "spaces must be stripped")

    def test_a_missing_file_and_no_keychain_reports_both_attempts(self):
        with self.assertRaises(SystemExit) as caught:
            rd.smtp_password(
                {
                    "username": "a@b.c",
                    "keychain_service": "definitely-not-a-real-service",
                    "password_file": str(self.dir / "absent"),
                }
            )
        message = str(caught.exception)
        self.assertIn("keychain", message)
        self.assertIn("missing", message)

    def test_no_source_configured_is_refused(self):
        with self.assertRaises(SystemExit):
            rd.smtp_password({"username": "a@b.c"})

    def test_the_keychain_read_is_bounded(self):
        """A hang is the failure this bound exists to prevent."""
        self.assertLessEqual(rd.KEYCHAIN_TIMEOUT_SECONDS, 30)


class EmailSafeStylingTests(unittest.TestCase):
    """Styling has to survive a mail client, which is a narrower target than a browser.

    A `<style>` block is the natural way to write this and the wrong one: Gmail
    and Outlook strip it silently, so the document would look right everywhere
    it is developed and arrive unstyled in the one place it is read. Everything
    is therefore inline, and these pin that so a later refactor toward a
    stylesheet fails loudly instead of shipping.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))
        self._prev = _set_vault(self.vault)
        self.manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.brief = {
            "schema": 1,
            "date": "2099-01-30",
            "groups": [
                {"tier": 1, "kind": "closing", "heading": "需要开始处理 1 件", "folded": False,
                 "items": [{"text": "a forfeitable thing", "source": "finance/x.md:1"}]},
                {"tier": 3, "kind": "review", "heading": "review 债 2 项", "folded": True, "items": []},
            ],
            "warnings": ["deadline index missing"],
        }

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev)

    def test_no_stylesheet_of_any_kind(self):
        document = rd.render(self.manifest, {}, self.brief)
        self.assertNotIn("<style", document)
        self.assertNotIn("stylesheet", document)
        self.assertNotIn("@media", document)
        self.assertNotIn("@font-face", document)

    def test_no_external_asset_is_referenced(self):
        """A remote font or image is blocked, proxied, or slow. None are used."""
        document = rd.render(self.manifest, {}, self.brief)
        self.assertNotIn("fonts.googleapis", document)
        self.assertNotIn("<img", document)
        self.assertNotIn("background-image", document)

    def test_structural_elements_carry_inline_style(self):
        document = rd.render(self.manifest, {}, self.brief)
        for tag in ("<h1", "<h2", "<ul", "<li", "<hr"):
            with self.subTest(tag=tag):
                self.assertRegex(document, re.escape(tag) + r'[^>]*style="')

    def test_the_first_screen_is_the_only_accented_block(self):
        """Weight is the argument: one card, for the one thing read before work."""
        document = rd.render(self.manifest, {}, self.brief)
        self.assertEqual(document.count(rd._S_CARD), 1)

    def test_a_forfeitable_group_outweighs_a_folded_count(self):
        document = rd.render(self.manifest, {}, self.brief)
        hot = document.index(rd._S_GROUP_HOT)
        cool = document.index(rd._S_GROUP_COOL)
        self.assertLess(hot, cool, "tier 1 must render above the folded tiers")

    def test_a_brief_warning_is_visually_marked(self):
        document = rd.render(self.manifest, {}, self.brief)
        self.assertIn("deadline index missing", document)
        self.assertIn(rd._ACCENT, document)


class FoldTests(unittest.TestCase):
    """A long document is only safe to send if the reader can see where to stop.

    The agreement is five minutes; anything past that is an offer, not a demand.
    That requires a visible boundary, an honest price on each half, and a scan
    layer that makes sense with the rest unread.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))
        self._prev = _set_vault(self.vault)
        self.manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.overview = {"schema": 1, "headline": "一句话", "sections": [
            {"title": "信号", "bullets": [{"text": "cross-source movement"}]}]}
        self.picks = [
            {"path": "wiki/Old.md", "tier": "wiki", "title": "An Old Idea",
             "age_days": 400, "excerpt": "旧笔记的正文摘录。", "reviewed": True},
            {"path": "reflections/Private.md", "tier": "reflections", "title": "Sensitive",
             "age_days": 200, "excerpt": "must never appear", "reviewed": False},
        ]

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev)

    def _doc(self):
        return rd.render(self.manifest, self.overview, None, self.picks)

    def test_the_fold_is_drawn_and_priced(self):
        document = self._doc()
        self.assertIn("以上", document)
        self.assertIn("分钟读完", document)
        self.assertIn("按需", document)
        self.assertIn(rd._S_FOLD, document)

    def test_depth_sections_sit_below_the_fold(self):
        document = self._doc()
        fold = document.index(rd._S_FOLD)
        for heading in ("情报详读", "随机回顾", "Source index"):
            with self.subTest(heading=heading):
                self.assertGreater(document.index(heading), fold)

    def test_the_overview_sits_above_the_fold(self):
        document = self._doc()
        self.assertLess(document.index("信号"), document.index(rd._S_FOLD))

    def test_each_depth_section_carries_its_own_cost(self):
        document = self._doc()
        below = document[document.index(rd._S_FOLD):]
        self.assertGreaterEqual(below.count(rd._S_COST), 2)

    def test_an_unreviewed_pick_never_reaches_the_page(self):
        """Delivery is the last place this rule can still be enforced."""
        document = self._doc()
        self.assertIn("An Old Idea", document)
        self.assertNotIn("must never appear", document)
        self.assertNotIn("Sensitive", document)

    def test_the_deep_read_is_the_body_and_the_index_stays_navigation(self):
        """This is the section that earns the document its length."""
        deep = rd._TAGS.sub("", rd._render_deep_read(self.manifest))
        index = rd._TAGS.sub("", rd._render_source(next(rd.iter_sources(self.manifest))[1]))
        self.assertGreater(len(deep), len(index))
        self.assertGreater(len(deep), rd._INDEX_EXCERPT_CAP)


class ReadingCostTests(unittest.TestCase):
    def test_empty_text_costs_nothing(self):
        self.assertEqual(rd.reading_minutes(""), 0)
        self.assertEqual(rd.reading_minutes("<div></div>"), 0)

    def test_any_real_text_costs_at_least_a_minute(self):
        """Rounding to zero would read as "free", which no section is."""
        self.assertEqual(rd.reading_minutes("<p>短</p>"), 1)

    def test_markup_is_not_counted_as_prose(self):
        bare = rd.reading_minutes("字" * 700)
        wrapped = rd.reading_minutes(f'<div style="{rd._S_WRAP}"><p>{"字" * 700}</p></div>')
        self.assertEqual(bare, wrapped)

    def test_cjk_and_latin_are_priced_separately(self):
        self.assertGreater(rd.reading_minutes("字" * 1000), 1)
        self.assertGreaterEqual(rd.reading_minutes(" ".join(["word"] * 700)), 3)


class ArticleSectionTests(unittest.TestCase):
    """Breadth over depth, and never a bare title.

    Attaching article bodies was measured at ~110,000 characters for three
    pieces: five times the depth budget and past the size where a mail client
    clips the message. More pieces with a real abstract each costs a fraction of
    that and gives a larger surface to choose from.
    """

    def _one(self, **over):
        base = {
            "title": "A Paper",
            "url": "https://read.readwise.io/read/abc",
            "minutes": "13 mins",
            "source": "example.com",
            "why": "服务 multimodal 方向",
            "abstract": "这篇讲的是把长视频按 GOP 分块存储并加帧级索引。",
        }
        base.update(over)
        return base

    def test_a_full_entry_renders_link_meta_why_and_abstract(self):
        html = rd._render_articles([self._one()])
        self.assertIn('href="https://read.readwise.io/read/abc"', html)
        self.assertIn("A Paper", html)
        self.assertIn("13 mins", html)
        self.assertIn("服务 multimodal 方向", html)
        self.assertIn("GOP 分块存储", html)

    def test_an_entry_without_an_abstract_is_dropped(self):
        """A bare title asks the reader to open it to learn whether to open it."""
        self.assertEqual(rd._render_articles([self._one(abstract="")]), "")
        self.assertEqual(rd._render_articles([self._one(abstract="   ")]), "")

    def test_an_entry_without_a_title_is_dropped(self):
        self.assertEqual(rd._render_articles([self._one(title="")]), "")

    def test_optional_fields_degrade_rather_than_break(self):
        html = rd._render_articles([self._one(url="", minutes="", source="", why="")])
        self.assertIn("A Paper", html)
        self.assertNotIn("<a ", html)

    def test_article_text_cannot_inject_markup(self):
        html = rd._render_articles(
            [self._one(title="<script>x</script>", abstract="<b>not bold</b>")]
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>not bold</b>", html)

    def test_articles_sit_above_the_fold(self):
        """They are the decision surface, which is what the scan layer is for."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            previous = _set_vault(vault)
            try:
                manifest = rd.collect(vault, mode="weekly", until="2099-01-30")
                document = rd.render(
                    manifest, {"schema": 1, "articles": [self._one()]}, None, []
                )
                self.assertLess(document.index("A Paper"), document.index(rd._S_FOLD))
            finally:
                _restore_vault(previous)


class MarkdownSubsetTests(unittest.TestCase):
    """Routine reports are Markdown and are data, never instruction."""

    def test_content_is_escaped_before_any_pattern_runs(self):
        html, _ = rd.markdown_to_html("# <script>alert(1)</script>\n\ntext")
        self.assertNotIn("<script>", html)

    def test_headings_lists_and_tables_survive(self):
        html, _ = rd.markdown_to_html(
            "## Findings\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        )
        self.assertIn("Findings", html)
        self.assertIn("<li", html)
        self.assertIn("<table", html)
        self.assertIn("<th", html)

    def test_images_are_removed_not_rendered(self):
        """Mail clients block remote images; they cost bytes and show a box."""
        html, _ = rd.markdown_to_html("![alt](https://example.com/x.png)\n\nreal text")
        self.assertNotIn("<img", html)
        self.assertNotIn("example.com/x.png", html)
        self.assertIn("real text", html)

    def test_a_long_report_is_truncated_and_says_so(self):
        html, truncated = rd.markdown_to_html("word " * 5000, limit=500)
        self.assertTrue(truncated)
        self.assertLess(len(html), 3000)

    def test_a_short_report_is_not_flagged_truncated(self):
        _html, truncated = rd.markdown_to_html("just a line", limit=500)
        self.assertFalse(truncated)


class AttentionBudgetTests(unittest.TestCase):
    """A routine earns space by being worth reading, not by being verbose.

    The cap is enforced by the renderer, not trusted to whoever writes the
    summary. A budget that depends on the writer's restraint is not a budget:
    one chatty collector crowds out five terse ones and the reader pays every
    morning without ever seeing why.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))
        self._prev = _set_vault(self.vault)
        self.manifest = rd.collect(self.vault, mode="weekly", until="2099-01-30")
        self.path = next(rd.iter_sources(self.manifest))[1]["path"]

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev)

    def _render(self, lines: int, cap: int | None = None):
        if cap is not None:
            for _lane, source in rd.iter_sources(self.manifest):
                source["max_lines"] = cap
        summary = "\n".join(f"第 {n} 行" for n in range(1, lines + 1))
        return rd._render_routine_briefs(
            [{"path": self.path, "summary": summary}], self.manifest
        )

    def test_a_summary_inside_its_budget_is_untouched(self):
        html = self._render(3, cap=5)
        self.assertIn("第 3 行", html)
        self.assertNotIn("已截至", html)

    def test_an_overlong_summary_is_cut_and_says_so(self):
        html = self._render(12, cap=5)
        self.assertIn("第 5 行", html)
        self.assertNotIn("第 6 行", html)
        self.assertIn("已截至 5 行", html)

    def test_the_registry_supplies_the_cap(self):
        self.assertEqual(
            {s["label"]: s["max_lines"] for _l, s in rd.iter_sources(self.manifest)}[
                "daily feed digest"
            ],
            rd.DEFAULT_ROUTINE_LINES,
        )

    def test_an_empty_summary_yields_no_entry(self):
        self.assertEqual(
            rd._render_routine_briefs([{"path": self.path, "summary": "  "}], self.manifest),
            "",
        )

    def test_summary_text_cannot_inject_markup(self):
        html = rd._render_routine_briefs(
            [{"path": self.path, "summary": "<script>x</script>"}], self.manifest
        )
        self.assertNotIn("<script>", html)

    def test_briefs_sit_above_the_fold_and_bodies_below(self):
        overview = {"schema": 1, "routines": [{"path": self.path, "summary": "一行摘要"}]}
        document = rd.render(self.manifest, overview, None, [])
        self.assertLess(document.index("routine 摘要"), document.index(rd._S_FOLD))
        self.assertGreater(document.index("情报详读"), document.index(rd._S_FOLD))


class SharedDirectoryTests(unittest.TestCase):
    """Acks are a per-directory mark; several routines can share a directory.

    Selecting per routine let a batch of one routine's oldest files advance the
    mark past another routine's older, never-shown files. The unit for unacked
    selection is therefore the directory, and `ack` names what it would hide.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))
        self._prev_ov = _set_vault(self.vault)
        registry = self.vault / "_meta" / "routine_watch.toml"
        registry.write_text(
            registry.read_text(encoding="utf-8")
            + '\n[[routine]]\nname = "extra-scan"\nlabel = "extra scan"\n'
            'output_dir = "finance/signals"\nfile_pattern = "*-extra.md"\n',
            encoding="utf-8",
        )
        signals = self.vault / "finance" / "signals"
        (signals / "2099-01-20-extra.md").write_text("## older extra\n", encoding="utf-8")
        (signals / "2099-01-27-extra.md").write_text("## newer extra\n", encoding="utf-8")
        # Every other directory is fully acked so the batch comes from here.
        (self.vault / "_meta" / "routine_acks.json").write_text(
            json.dumps({"inbox/feed": "zzz", "career/scans": "zzz", "agent-findings": "zzz"}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()
        _restore_vault(self._prev_ov)

    def test_unacked_batch_walks_a_shared_directory_oldest_first(self):
        manifest = rd.collect(self.vault, mode="weekly", unacked=True, max_files=2)
        names = sorted(
            Path(s["path"]).name for lane in manifest["lanes"] for s in lane["sources"]
        )
        self.assertEqual(names, ["2099-01-20-extra.md", "2099-01-25-monitor.md"])
        self.assertTrue(manifest["truncated"])
        # The mark after acking this batch is exactly its last file: nothing
        # older than it in this directory was skipped.
        self.assertEqual(manifest["acks"], {"finance/signals": "2099-01-25-monitor.md"})

    def test_ack_names_the_files_it_would_hide(self):
        import contextlib
        import io

        manifest = {
            "acks": {"finance/signals": "2099-01-27-extra.md"},
            "lanes": [{"sources": [{"path": "finance/signals/2099-01-27-extra.md"}]}],
        }
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rd.ack(self.vault, manifest, dry_run=True)
        out = buffer.getvalue()
        self.assertIn("finance/signals: ∅ → 2099-01-27-extra.md", out)
        self.assertIn("also marks 1 unshown policy monitor file(s)", out)
        self.assertIn("also marks 1 unshown extra scan file(s)", out)


class MastheadAndContextTests(unittest.TestCase):
    """Weather and harness quota in the masthead; provenance in the colophon."""

    def setUp(self):
        self.manifest = {
            "schema": rd.MANIFEST_SCHEMA,
            "mode": "daily",
            "window": {"since": "2099-01-30", "until": "2099-01-30"},
            "generated": "2099-01-30T06:22:00",
            "counts": {"files": 1, "bytes": 2048, "updates": 0},
            "health": {"reported": 4, "declared": 19, "completed": 2, "failed": 0, "review_debt": 149},
            "lanes": [
                {
                    "lane": "Tech feed",
                    "files": 1,
                    "sources": [
                        {
                            "path": "inbox/feed/2099-01-30-feed.md",
                            "label": "daily feed digest",
                            "date": "2099-01-30",
                            "headline": "Daily Feed Digest",
                            "items": [
                                {"title": "First item title", "url": "https://example.com/one", "note": "A blurb"}
                            ],
                            "units": [],
                            "excerpt": "",
                        }
                    ],
                },
                {
                    "lane": "Finance",
                    "files": 1,
                    "sources": [
                        {
                            "path": "finance/signals/2099-01-25-monitor.md",
                            "label": "policy monitor",
                            "date": "2099-01-25",
                            "headline": "rate decision signal",
                            "items": [],
                            "units": [],
                            "excerpt": "The rate corridor was held unchanged on a split vote.",
                        }
                    ],
                },
            ],
        }
        self.context = {
            "schema": rd.CONTEXT_SCHEMA,
            "date": "2099-01-30",
            "weather": {
                "place": "Lisbon",
                "tmin": 13,
                "tmax": 25,
                "summary": "少云",
                "precip_probability": 2,
                "hours": [{"hour": 9, "temp": 18}, {"hour": 18, "temp": 21}],
                "date": "2099-01-30",
            },
            "quota": [
                {
                    "name": "Claude Code",
                    "window": "Fable · 7d",
                    "left_percent": 83,
                    "level": "ok",
                    "reset_relative": "1 天 2 小时后重置",
                    "snapshot_age_hours": 0.5,
                },
                {
                    "name": "Codex",
                    "window": "prolite · 7d",
                    "left_percent": 15,
                    "level": "critical",
                    "reset_relative": "4 小时后重置",
                    "snapshot_age_hours": 3.0,
                },
            ],
            "warnings": [],
        }

    def test_weather_sits_in_the_masthead_and_provenance_in_the_colophon(self):
        document = rd.render(self.manifest, None, None, None, self.context)
        self.assertIn("Lisbon", document)
        self.assertIn("13–25°C", document)
        self.assertIn("9:00 18°", document)
        self.assertLess(document.index("Lisbon"), document.index("Source index"))
        self.assertLess(document.index("Source index"), document.index("KB 源文本"))
        self.assertLess(document.index("Source index"), document.index("生成于 06:22"))

    def test_quota_bar_shows_the_remaining_share_in_its_level_colour(self):
        document = rd.render(self.manifest, None, None, None, self.context)
        self.assertIn("Harness 周额度", document)
        self.assertIn('width="83%"', document)
        self.assertIn(f"color:{rd._OK};\">剩 83%", document)
        self.assertIn("1 天 2 小时后重置", document)
        self.assertIn('width="15%"', document)
        self.assertIn(f"color:{rd._URGENT};\">剩 15%", document)
        self.assertIn("快照 3.0h 前", document)

    def test_context_is_optional_and_warnings_are_visible(self):
        plain = rd.render(self.manifest)
        self.assertNotIn("Harness 周额度", plain)
        warned = rd.render(self.manifest, None, None, None, {"quota": [], "warnings": ["claude quota: no snapshot"]})
        self.assertIn("claude quota: no snapshot", warned)

    def test_unknown_context_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                rd.load_context(path)

    def test_health_strip_borrows_recurring_and_review_counts_from_the_brief(self):
        brief = {
            "schema": 1,
            "date": "2099-01-30",
            "groups": [
                {"tier": 3, "kind": "recurring", "heading": "recurring: 9 条逾期, 4 条刚到期", "folded": True, "items": []},
                {
                    "tier": 3,
                    "kind": "review",
                    "heading": "review 债 3 项",
                    "folded": False,
                    "items": [{"text": "autoevo_pending"}, {"text": "aggregate_freshness"}, {"text": "routine_outputs"}],
                },
            ],
            "warnings": [],
        }
        strip = rd._render_health(self.manifest["health"], brief)
        self.assertIn("recurring 逾期", strip)
        self.assertIn(f'color:{rd._URGENT};">9<', strip)
        self.assertIn("review 债", strip)
        self.assertIn(">3<", strip)
        self.assertEqual(strip.count("<td"), 6)
        self.assertEqual(rd._render_health(self.manifest["health"]).count("<td"), 4)

    def test_ledger_due_column_encodes_urgency(self):
        brief = {
            "schema": 1,
            "date": "2099-01-30",
            "groups": [
                {
                    "tier": 1,
                    "kind": "closing_lead",
                    "heading": "需要开始处理 1 件",
                    "folded": False,
                    "items": [{"text": "Hotel credit", "days_left": 42, "source": "finance/x.md:1"}],
                },
                {
                    "tier": 2,
                    "kind": "todo",
                    "heading": "TODO 到期 3 件",
                    "folded": False,
                    "items": [
                        {"text": "Notarize form", "days_left": 0},
                        {"text": "Review status", "days_left": 4},
                        {"text": "Late thing", "days_left": -2},
                    ],
                },
            ],
            "warnings": [],
        }
        card = rd._render_brief(brief)
        self.assertIn(f'color:{rd._ACCENT};">42d', card)
        self.assertIn(f'color:{rd._URGENT};font-weight:600;">今天', card)
        self.assertIn(f'color:{rd._MUTED};">4d', card)
        self.assertIn("逾期 2d", card)
        self.assertIn("今日 <span", card)
        self.assertIn("· 4 件", card)
        self.assertIn("x.md:1", card)


class FrontierAndCuratedDepthTests(unittest.TestCase):
    def setUp(self):
        self.manifest = MastheadAndContextTests.setUp.__get__(self)() or self.manifest

    def _labs(self, sweep_date):
        return {
            "sweep_date": sweep_date,
            "drift_count": 0,
            "promotion_count": 1,
            "signals": [
                {"lab": "Example Lab", "category": "模型发布", "tier": 1, "text": "Atlas 早期访问。", "url": "https://example.com/atlas"}
            ],
            "watchlist_note": "本周无使命漂移。",
        }

    def test_frontier_renders_the_table_on_the_sweep_day_and_the_day_after(self):
        for sweep in ("2099-01-30", "2099-01-29"):
            document = rd.render(self.manifest, {"schema": 1, "frontier_labs": self._labs(sweep)})
            self.assertIn("前沿实验室", document)
            self.assertIn("1 条信号 · 0 漂移 · 1 晋级 · 周扫 " + sweep[5:], document)
            self.assertIn("Example Lab", document)
            self.assertIn('href="https://example.com/atlas"', document)
            self.assertIn("本周无使命漂移。", document)
            self.assertLess(document.index("前沿实验室"), document.index(rd._S_FOLD))

    def test_frontier_collapses_to_counts_on_later_days(self):
        document = rd.render(self.manifest, {"schema": 1, "frontier_labs": self._labs("2099-01-25")})
        self.assertIn("1 条信号 · 0 漂移 · 1 晋级 · 周扫 01-25", document)
        self.assertNotIn("Example Lab", document)
        self.assertIn("已随当日 digest 报告", document)

    def test_frontier_is_absent_without_the_field(self):
        self.assertNotIn("前沿实验室", rd.render(self.manifest, {"schema": 1}))

    def test_curated_deep_read_replaces_raw_bodies_and_caps_facts_at_two(self):
        overview = {
            "schema": 1,
            "deep_read": {
                "total": 5,
                "entries": [
                    {
                        "title": "HBM 层数增加压缩良率",
                        "url": "https://example.com/hbm",
                        "facts": ["第一点。", "第二点。", "第三点不该出现。"],
                        "why": "支撑 **MU** 的定价。",
                    }
                ],
            },
        }
        document = rd.render(self.manifest, overview)
        self.assertIn("信号精选 · 1 / 5", document)
        self.assertIn("第一点。", document)
        self.assertIn("第二点。", document)
        self.assertNotIn("第三点不该出现。", document)
        self.assertRegex(document, r"<(b|strong)[^>]*>MU</")
        self.assertIn('href="https://example.com/hbm"', document)
        # The raw body is gone from the depth layer; the source index below it
        # still carries its excerpt, which is navigation, not depth.
        depth = document[document.index(rd._S_FOLD):document.index("Source index")]
        self.assertNotIn("The rate corridor was held", depth)
        self.assertNotIn(rd._S_DEEP_ITEM, depth)
        # The feed's own items follow, deterministically, from the manifest.
        self.assertIn("科技动态 · 1", document)
        self.assertIn('href="https://example.com/one"', document)
        self.assertIn("A blurb", document)
        self.assertIn("信号 1 / 5 · 科技动态 1", document)
        self.assertGreater(document.index("信号精选"), document.index(rd._S_FOLD))

    def _with_research_lane(self) -> dict:
        manifest = json.loads(json.dumps(self.manifest))
        manifest["lanes"].insert(0, {
            "lane": "Research", "files": 1,
            "sources": [{
                "path": "research/labs/2099-01-30-sweep.md", "label": "lab sweep",
                "date": "2099-01-30", "headline": "weekly sweep",
                "items": [], "units": [], "excerpt": "A lab shipped a memory paper.",
            }],
        })
        return manifest

    def _finance_only_pick(self, lane=None) -> dict:
        entry = {"title": "HBM 良率", "url": "https://example.com/hbm", "facts": ["一。"], "why": "为什么。"}
        if lane:
            entry["lane"] = lane
        return {"schema": 1, "deep_read": {"total": 3, "entries": [entry]}}

    def test_depth_that_skips_research_on_a_research_window_is_flagged(self):
        """Guard for the finance-dominated pick: 2026-09-01 shipped three finance
        entries and zero research on a window that had a research source."""
        manifest = self._with_research_lane()
        gap = rd.deep_read_lane_gap(self._finance_only_pick()["deep_read"], manifest)
        self.assertIsNotNone(gap)
        self.assertIn("1 个 Research 来源", gap)
        document = rd.render(manifest, self._finance_only_pick())
        self.assertIn("! 情报精选没有 Research 条目", document)
        self.assertGreater(document.index("! 情报精选"), document.index("情报详读"))

    def test_research_entry_or_research_free_window_is_silent(self):
        manifest = self._with_research_lane()
        self.assertIsNone(rd.deep_read_lane_gap(self._finance_only_pick("Research")["deep_read"], manifest))
        self.assertIsNone(rd.deep_read_lane_gap(self._finance_only_pick()["deep_read"], self.manifest))
        self.assertIsNone(rd.deep_read_lane_gap({"total": 0, "entries": []}, manifest))
        self.assertNotIn("! 情报精选", rd.render(manifest, self._finance_only_pick("Research")))

    def test_without_curation_the_raw_fallback_still_renders(self):
        document = rd.render(self.manifest, {"schema": 1})
        self.assertNotIn("信号精选", document)
        self.assertIn("情报详读", document)
        self.assertIn("The rate corridor was held", document)

    def test_section_headings_carry_their_bullet_count(self):
        overview = {"schema": 1, "sections": [{"title": "需要的决策", "bullets": []}, {"title": "信号", "bullets": [{"text": "a"}, {"text": "b"}]}]}
        document = rd.render(self.manifest, overview)
        self.assertIn(f'需要的决策<span style="{rd._S_H2_N}">0</span>', document)
        self.assertIn(f'信号<span style="{rd._S_H2_N}">2</span>', document)

    def test_article_heading_counts_pieces_and_minutes(self):
        articles = [
            {"title": "A", "url": "https://example.com/a", "abstract": "x", "minutes": 10},
            {"title": "B", "url": "https://example.com/b", "abstract": "y", "minutes": 13},
            {"title": "dropped", "abstract": ""},
        ]
        self.assertIn("2 篇 · 23 分钟", rd._articles_badge(articles))
        self.assertEqual(rd._articles_badge([]), "")


class ReviewFollowUpTests(unittest.TestCase):
    """Cross-file contracts and the fail-closed paths the system review asked for."""

    def test_schema_constants_agree_across_modules(self):
        import daily_brief
        import daily_context

        self.assertEqual(rd.BRIEF_SCHEMA, daily_brief.BRIEF_SCHEMA)
        self.assertEqual(rd.CONTEXT_SCHEMA, daily_context.CONTEXT_SCHEMA)

    def test_malformed_overview_section_is_skipped_not_fatal(self):
        manifest = {"schema": rd.MANIFEST_SCHEMA, "mode": "daily", "window": {"since": "2099-01-30", "until": "2099-01-30"}, "counts": {"files": 0, "bytes": 0}, "lanes": []}
        document = rd.render(manifest, {"schema": 1, "sections": ["not an object", {"title": "信号", "bullets": [{"text": "ok"}]}]})
        self.assertIn("malformed overview section skipped", document)
        self.assertIn("信号", document)

    def test_frontier_fails_closed_without_a_digest_date(self):
        labs = {"sweep_date": "2099-01-30", "signals": [{"lab": "Example Lab", "text": "Atlas."}]}
        self.assertNotIn("Example Lab", rd._render_frontier(labs, ""))
        self.assertIn("Example Lab", rd._render_frontier(labs, "2099-01-30"))

    def test_brief_counts_round_trip_through_daily_brief_wording(self):
        """The strip parses daily_brief's own heading, so build that heading with
        daily_brief itself rather than a hand-typed imitation."""
        import types
        import daily_brief
        from datetime import date as _date, timedelta as _td

        today = _date(2099, 1, 30)

        class Row:
            def __init__(self, slug, days, line):
                self.slug, self._days, self.line = slug, days, line

            def status(self, _today):
                return "overdue" if self._days < 0 else "due-soon"

            def days_until_due(self, _today):
                return self._days

            def next_due(self):
                return today + _td(days=self._days)

            def every_str(self):
                return "30d"

        fake = types.ModuleType("recurring")
        fake.parse_file = lambda: [Row("a", -40, 1), Row("b", -3, 2), Row("c", 5, 3)]
        previous = sys.modules.get("recurring")
        sys.modules["recurring"] = fake
        try:
            groups = daily_brief.load_recurring(Path("."), today, [])
        finally:
            if previous is None:
                del sys.modules["recurring"]
            else:
                sys.modules["recurring"] = previous
        self.assertEqual(len(groups), 1)
        brief = {"groups": [{"kind": groups[0].kind, "heading": groups[0].display_heading(), "items": []}]}
        self.assertEqual(rd._brief_counts(brief)["recurring_overdue"], 2)

    def test_password_file_under_the_vault_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            secret = vault / "_meta" / "smtp.txt"
            secret.write_text("app password\n", encoding="utf-8")
            secret.chmod(0o600)
            previous = _set_vault(vault)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    rd.smtp_password({"username": "someone@example.com", "password_file": str(secret)})
            finally:
                _restore_vault(previous)
            self.assertIn("under $OV", str(ctx.exception))
