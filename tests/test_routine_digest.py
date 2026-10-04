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
import re
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Event
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
REPRESENTATIVE_FIXTURE = REPO_ROOT / "tests/fixtures/digest/representative.json"

import _paths  # noqa: E402
import routine_digest as rd  # noqa: E402
import routine_collect as rc  # noqa: E402
import routine_digest_core as core  # noqa: E402
import routine_mail as rm  # noqa: E402
import routine_render as rr  # noqa: E402


def representative_digest_inputs(mode: str = "daily") -> tuple[dict, dict, dict | None, list, dict]:
    """Shareable, synthetic inputs for renderer regression and manual smoke use."""
    if mode not in {"daily", "weekly"}:
        raise ValueError(f"unsupported representative digest mode: {mode}")
    payload = json.loads(REPRESENTATIVE_FIXTURE.read_text(encoding="utf-8"))
    manifest = payload["manifest"]
    overview = payload["overview"]
    brief = payload["brief"]
    if mode == "weekly":
        manifest["mode"] = "weekly"
        manifest["window"] = {"since": "2099-01-24", "until": "2099-01-30"}
        overview["sections"] = [
            section for section in overview["sections"] if section["title"] != rr.DECISION_SECTION
        ]
        overview.pop("articles", None)
        brief = None
    return manifest, overview, brief, payload["retrospect"], payload["context"]


class ParsedNode:
    """Tiny stdlib DOM for assertions about rendered outcomes, not templates."""

    def __init__(self, tag: str, attrs: list[tuple[str, str | None]] | None = None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children: list[ParsedNode | str] = []

    @property
    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text for child in self.children)

    def find_all(self, tag: str | None = None, *, class_name: str | None = None) -> list[ParsedNode]:
        matches: list[ParsedNode] = []
        if (tag is None or self.tag == tag) and (
            class_name is None or class_name in self.attrs.get("class", "").split()
        ):
            matches.append(self)
        for child in self.children:
            if isinstance(child, ParsedNode):
                matches.extend(child.find_all(tag, class_name=class_name))
        return matches


class ParsedDocument(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, document: str):
        super().__init__(convert_charrefs=True)
        self.root = ParsedNode("document")
        self.stack = [self.root]
        self.feed(document)
        self.close()

    @property
    def text(self) -> str:
        return self.root.text

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = ParsedNode(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(ParsedNode(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


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
version = 1

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
label = "maintenance output"
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
    (vault / "_tools/routines").mkdir(parents=True)
    (vault / "_tools/routines/registry.toml").write_text(WATCH_TOML, encoding="utf-8")
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


class VaultCase(unittest.TestCase):
    """Each scenario gets isolated data and restores process-local path caches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = build_vault(Path(self.tmp.name))
        self.addCleanup(_restore_vault, _set_vault(self.vault))
        self.manifest = rc.collect(self.vault, mode="weekly", until="2099-01-30")

    def _brief(self, *items, **group):
        return {
            "schema": 1,
            "date": "2099-01-30",
            "groups": [{"tier": 1, "kind": "closing_lead", "heading": "需要开始处理 1 件",
                        "folded": False, "items": list(items), **group}],
            "warnings": [],
        }

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/routine_digest.py", *args],
            cwd=REPO_ROOT, env={**os.environ, "OV": str(self.vault)},
            capture_output=True, text=True, timeout=120,
        )


class ExtractionTests(unittest.TestCase):
    def test_file_date_from_any_position(self):
        self.assertEqual(
            rc.file_date(Path("2099-01-30-feed.md"))[0].isoformat(), "2099-01-30"
        )
        self.assertEqual(
            rc.file_date(Path("role_scan_2099-01-28.md"))[0].isoformat(), "2099-01-28"
        )
        self.assertEqual(
            rc.file_date(Path("weekly-sweep-2099-01-22.md"))[0].isoformat(), "2099-01-22"
        )

    def test_file_date_falls_back_to_mtime_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no-date-here.md"
            path.write_text("x", encoding="utf-8")
            _, source = rc.file_date(path)
            self.assertEqual(source, "mtime")

    def test_multi_signal_extraction_preserves_boundaries_and_payload(self):
        units = rc.split_units(SIGNAL_REPORT)
        self.assertEqual(
            [meta["slug"] for meta, _ in units],
            ["rate-decision-signal", "trade-order-signal"],
        )
        hidden = re.sub(r"^---\n((?:\w+: .*\n)+)---$", r"<!--\n\1-->", SIGNAL_REPORT, flags=re.M)
        self.assertIn("<!--\ndate: 2099-01-25", hidden)
        self.assertEqual(rc.split_units(hidden), units)

        first_body = units[0][1]
        self.assertIn("split vote", first_body)
        self.assertNotIn("trade orders", first_body)
        headline = rc.extract_headline({}, SIGNAL_REPORT, units)
        self.assertTrue(headline.startswith("2 signals:"), headline)
        self.assertIn("rate decision signal", headline)
        self.assertNotIn("Collection status", headline)

        excerpt = rc.extract_excerpt(units[0][1], 400)
        self.assertIn("valuation", excerpt)
        self.assertNotIn("split vote", excerpt)
        self.assertNotIn("**", excerpt)
        self.assertNotIn("](", excerpt)
        self.assertIn("capex complex", excerpt)

    def test_ordinary_reports_do_not_create_units_and_skip_bookkeeping_titles(self):
        shapes = (
            ("feed", TECH_DIGEST, "Daily Feed Digest — 2099-01-30"),
            ("bookkeeping", "## Collection status\n\n- coverage note\n\n## Real Finding\n\ntext\n", "Real Finding"),
            ("rules", "para one\n\n---\n\npara two\n\n---\n\npara three\n", None),
        )
        for name, text, title in shapes:
            with self.subTest(shape=name):
                self.assertEqual(rc.split_units(text), [])
                if title:
                    meta, body = rc.parse_frontmatter(text)
                    self.assertEqual(rc.extract_headline(meta, body, []), title)

    def test_excerpt_truncates_at_a_sentence_boundary(self):
        text = "## Why This Matters\n\n" + ("Sentence one is here. " * 20)
        excerpt = rc.extract_excerpt(text, 100)
        self.assertLessEqual(len(excerpt), 110)
        self.assertTrue(excerpt.endswith("…"))

    def test_items_come_from_lists_not_prose(self):
        items = rc.extract_items(TECH_DIGEST, 15)
        self.assertEqual([i["url"] for i in items], ["https://example.com/one", "https://example.com/two"])
        prose = "See [a citation](https://example.com/cited) in this paragraph.\n"
        self.assertEqual(rc.extract_items(prose, 15), [])

    def test_items_respect_the_cap(self):
        body = "\n".join(f"{n}. [t{n}](https://example.com/{n})" for n in range(20))
        self.assertEqual(len(rc.extract_items(body, 5)), 5)


class ParsingContractTests(unittest.TestCase):
    """Pins the markdown contract the digest depends on.

    `routine_collect` parses ledgers and reports by hand. Any replacement
    (markdown-it, a table library) must reproduce every behavior below; the
    first test also guards a value persisted in $OV.
    """

    def test_row_key_is_a_stable_hash_of_the_normalized_values(self):
        # Persisted as the daily delivery cursor. Changing it re-delivers or
        # skips updates, so this value may only move with a state migration.
        self.assertEqual(
            rc.row_key("status-ledger", ["Date", "Note"], ["2099-01-30", "shipped"]),
            "1e90cbb582ff9cd7269fe3959a98a78441eee86308748169503a09f59260e569",
        )

    def test_row_key_survives_reformatting(self):
        # Pipe alignment, cell padding, and whitespace runs must not move it.
        self.assertEqual(
            rc.row_key("l", ["Date ", "Note"], ["  2099-01-30 ", "shipped   now"]),
            rc.row_key("l", ["Date", "Note"], ["2099-01-30", "shipped now"]),
        )
        self.assertNotEqual(
            rc.row_key("l", ["Date", "Note"], ["2099-01-30", "a"]),
            rc.row_key("l", ["Date", "Note"], ["2099-01-30", "b"]),
        )

    def test_legacy_row_id_still_resolves_a_pre_normalization_cursor(self):
        # Read-only dual path: a cursor written before row_key existed must
        # still match, so no $OV state migration is required.
        self.assertEqual(
            rc.legacy_row_id("status-ledger", "| 2099-01-30 | shipped |"),
            "a64623a6523c0b639e2630747f14abb48b9de51556246c4eb34610ce2224beb0",
        )

    def test_table_drops_rows_whose_cell_count_disagrees_with_the_header(self):
        text = (
            "## Ledger\n"
            "| Date | Note |\n"
            "| --- | --- |\n"
            "| 2099-01-30 | kept |\n"
            "| 2099-01-31 |\n"
            "| 2099-02-01 | kept | extra |\n"
        )
        headers, rows, rejected = rc._markdown_table(text, "Ledger")
        self.assertEqual(headers, ["Date", "Note"])
        self.assertEqual([cells[0] for _raw, cells in rows], ["2099-01-30"])
        # dropped rows are reported, never silent
        self.assertEqual(rejected, ["| 2099-01-31 |", "| 2099-02-01 | kept | extra |"])

    def test_table_needs_a_separator_row_matching_the_header_arity(self):
        text = "## Ledger\n| Date | Note |\n| --- |\n| 2099-01-30 | x |\n"
        self.assertEqual(rc._markdown_table(text, "Ledger"), ([], [], []))

    def test_table_stops_at_the_first_non_pipe_line(self):
        text = (
            "## Ledger\n"
            "| Date | Note |\n"
            "| --- | --- |\n"
            "| 2099-01-30 | first |\n"
            "\n"
            "| 2099-01-31 | after the break |\n"
        )
        _headers, rows, _rejected = rc._markdown_table(text, "Ledger")
        self.assertEqual([cells[1] for _raw, cells in rows], ["first"])

    def test_table_rows_carry_their_raw_line_verbatim(self):
        text = "## Ledger\n| Date | Note |\n| --- | --- |\n|  2099-01-30 | spaced  |\n"
        _headers, rows, _rejected = rc._markdown_table(text, "Ledger")
        self.assertEqual(rows[0][0], "|  2099-01-30 | spaced  |")

    def test_prose_skips_fences_tables_headings_quotes_and_stray_keys(self):
        chunk = (
            "kept one\n"
            "```\n"
            "dropped_code = 1\n"
            "```\n"
            "# heading\n"
            "| table | row |\n"
            "> quote\n"
            "---\n"
            "status: draft\n"
            "kept two\n"
        )
        self.assertEqual(rc._prose_lines(chunk), ["kept one", "kept two"])

    def test_prose_strips_list_markers_and_stops_at_the_budget(self):
        self.assertEqual(
            rc._prose_lines("- bullet\n* star\n+ plus\n1. first\n2) second\n"),
            ["bullet", "star", "plus", "first", "second"],
        )
        long_lines = "\n".join("x" * 300 for _ in range(20))
        self.assertLess(len(rc._prose_lines(long_lines)), 20)

    def test_split_sections_returns_the_whole_body_when_there_is_no_heading(self):
        self.assertEqual(rc._split_sections("just prose\n"), [("", "just prose\n")])
        self.assertEqual(
            [name for name, _body in rc._split_sections("# A\nx\n## B\ny\n")],
            ["A", "B"],
        )


class WindowTests(unittest.TestCase):
    def test_default_windows_are_inclusive(self):
        for mode, since, days in (("weekly", "2099-01-24", 7), ("daily", "2099-01-30", 1)):
            with self.subTest(mode=mode):
                start, end, span = rc.resolve_window(mode, days=None, since=None, until="2099-01-30")
                self.assertEqual((start.isoformat(), end.isoformat(), span), (since, "2099-01-30", days))

    def test_effective_date_rolls_back_before_three_am(self):
        from datetime import datetime

        self.assertEqual(
            rc.effective_date(datetime(2099, 1, 31, 1, 30)).isoformat(), "2099-01-30"
        )
        self.assertEqual(
            rc.effective_date(datetime(2099, 1, 31, 9, 0)).isoformat(), "2099-01-31"
        )

    def test_since_after_until_is_rejected(self):
        with self.assertRaises(SystemExit):
            rc.resolve_window("weekly", days=None, since="2099-01-30", until="2099-01-24")


class CollectTests(VaultCase):
    def test_weekly_collection_selects_window_and_maintenance_policy(self):
        names = sorted(s["name"] for _, s in core.iter_sources(self.manifest))
        self.assertEqual(
            names,
            [
                "2099-01-24-feed.md",
                "2099-01-25-monitor.md",
                "2099-01-30-feed.md",
                "role_scan_2099-01-28.md",
            ],
        )
        cases = (
            ("default exclusion", self.manifest, False),
            ("explicit inclusion", rc.collect(
                self.vault, mode="weekly", until="2099-01-30", include_maintenance=True
            ), True),
        )
        for name, manifest, included in cases:
            with self.subTest(case=name):
                source_names = [s["name"] for _, s in core.iter_sources(manifest)]
                self.assertEqual("autoevo-applied-2099-01-30.md" in source_names, included)
                self.assertEqual("maintenance output" in manifest["skipped_routines"], not included)

    def test_declared_digest_overrides(self):
        watch = self.vault / "_tools/routines/registry.toml"
        cases = (
            ("lane override", '*-feed.md', 'digest = { lane = "Research" }', "Research", True, None),
            ("declared exclusion", 'role_scan_*.md', "digest = { include = false }", "Career", False, "role scan"),
        )
        for name, pattern, declaration, lane, present, skipped in cases:
            with self.subTest(case=name):
                needle = f'file_pattern = "{pattern}"'
                watch.write_text(WATCH_TOML.replace(needle, f"{needle}\n{declaration}"), encoding="utf-8")
                manifest = rc.collect(self.vault, mode="weekly", until="2099-01-30")
                self.assertEqual(lane in [row["lane"] for row in manifest["lanes"]], present)
                if skipped:
                    self.assertIn(skipped, manifest["skipped_routines"])

    def test_unacked_mode_ignores_the_window(self):
        (self.vault / "_meta" / "routine_acks.json").write_text(
            json.dumps({"inbox/feed": "2099-01-24-feed.md"}), encoding="utf-8"
        )
        manifest = rc.collect(self.vault, mode="weekly", until="2098-01-01", unacked=True)
        names = sorted(s["name"] for _, s in core.iter_sources(manifest))
        self.assertEqual(
            names,
            [
                "2099-01-25-monitor.md",
                "2099-01-30-feed.md",
                "role_scan_2099-01-28.md",
            ],
        )

    def test_degraded_source_coverage_survives_curated_rendering(self):
        text = TECH_DIGEST.replace("item_count: 2", "item_count: 2\nstatus: degraded\nchannels_reached: 0/5")
        text = text.replace("## Items", "输入缺口：完整正文不可用。\n\n## Items")
        text = text.replace("A blurb about the first item.", "第一条中文摘要。")
        text = text.replace("Another blurb.", "第二条中文摘要。")
        (self.vault / "inbox/feed/2099-01-30-feed.md").write_text(text, encoding="utf-8")
        manifest = rc.collect(self.vault, mode="daily", days=1, until="2099-01-30")
        source = next(s for _, s in core.iter_sources(manifest) if s["routine"] == "feed-digest")
        self.assertEqual(source["meta"].get("channels_reached"), "0/5")
        self.assertIsNone(rr.feed_note_gap(manifest))
        overview = {"schema": 1, "deep_read": {"total": 1, "entries": [
            {"title": "精选", "lane": "Finance", "facts": ["事实"], "why": "关联"},
        ]}}
        document = ParsedDocument(rr.render(manifest, overview)).text
        self.assertIn("status=degraded", document)
        self.assertIn("channels_reached=0/5", document)
        self.assertIn("输入缺口：完整正文不可用。", document)
        self.assertGreater(document.index("status=degraded"), document.index("以上 "))
        source["meta"]["status"] = "complete"
        self.assertNotIn("status=complete", ParsedDocument(rr.render(manifest, overview)).text)

    def test_collected_provenance_and_default_lanes(self):
        manifest = self.manifest
        self.assertEqual(
            [lane["lane"] for lane in manifest["lanes"]], ["Tech feed", "Finance", "Career"]
        )
        source = next(
            s for _, s in core.iter_sources(manifest) if s["name"].endswith("monitor.md")
        )
        self.assertEqual(len(source["units"]), 2)
        self.assertNotIn("excerpt", source)
        self.assertEqual(
            source["units"][0]["source_url"], "https://example.com/minutes"
        )
        self.assertIn("valuation", source["units"][0]["excerpt"])

        self.assertEqual(manifest["acks"]["inbox/feed"], "2099-01-30-feed.md")

    def test_daily_updates_use_a_delivery_cursor_not_the_date_window(self):
        # The row was checked yesterday, after that morning's report. It must
        # land in today's daily artifact and then disappear from the next run.
        # --days 1 keeps this about the update cursor: the default daily
        # selection would also carry yesterday's undelivered feed file.
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-31", days=1)
        self.assertEqual(manifest["counts"]["files"], 0)
        self.assertEqual(len(manifest["updates"]), 2)
        self.assertEqual(manifest["updates"][0]["values"]["Period"], "2099-01")
        self.assertEqual(manifest["updates"][1]["values"]["Period"], "2099-02")

        rd.write(self.vault, rr.render(manifest), manifest, routine_name="digest-writer")
        state = json.loads(
            (self.vault / rc.DIGEST_UPDATES_STATE).read_text(encoding="utf-8")
        )
        self.assertEqual(state["daily"]["status-ledger"], manifest["updates"][-1]["id"])
        replay = rc.collect(self.vault, mode="daily", until="2099-02-01")
        self.assertEqual(replay["updates"], [])

    def test_backdated_update_does_not_move_the_delivery_cursor_backwards(self):
        ledger = self.vault / "personal/status-tracker.md"
        ledger.write_text(UPDATE_LEDGER.replace("| 2099-02 | 2099-01-30 |", "| 2099-02 | 2099-01-29 |"))
        self.assertEqual(rc.collect(self.vault, mode="daily", until="2099-01-29")["updates"], [])
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-31", days=1)
        self.assertEqual([item["sequence"] for item in manifest["updates"]], [1, 0])
        rd.write(self.vault, rr.render(manifest), manifest, routine_name="digest-writer")
        self.assertEqual(rc.collect(self.vault, mode="daily", until="2099-02-01")["updates"], [])

    def test_saved_manifests_cannot_regress_state_or_overwrite_a_newer_artifact(self):
        ledger = self.vault / "personal/status-tracker.md"
        rows = UPDATE_LEDGER.splitlines(keepends=True)
        ledger.write_text("".join(rows[:-1]))
        older = rc.collect(self.vault, mode="daily", until="2099-01-31")
        ledger.write_text(UPDATE_LEDGER)
        newer = rc.collect(self.vault, mode="daily", until="2099-01-31")
        target = self.vault / "inbox/digest/2099-01-31-daily-digest.html"
        rd.write(self.vault, "NEWER ARTIFACT", newer)
        before = (self.vault / rc.DIGEST_UPDATES_STATE).read_bytes()
        with self.assertRaisesRegex(SystemExit, "existing digest"):
            rd.write(self.vault, "OLDER ARTIFACT", older)
        self.assertEqual(target.read_text(), "NEWER ARTIFACT")
        self.assertEqual((self.vault / rc.DIGEST_UPDATES_STATE).read_bytes(), before)
        rd.write(self.vault, "HISTORICAL ARTIFACT", older, out=target.with_name("historical.html"))
        self.assertEqual(rc.collect(self.vault, mode="daily", until="2099-02-01")["updates"], [])

    def test_empty_or_recollected_manifest_cannot_replace_successful_output(self):
        ledger = self.vault / "personal/status-tracker.md"
        rows = UPDATE_LEDGER.splitlines(keepends=True)
        ledger.write_text("".join(rows[:-1]).replace("2099-01-30", "2099-01-29"))
        first = rc.collect(self.vault, mode="daily", until="2099-01-29")
        rd.write(self.vault, "FIRST DAY", first)
        older = rc.collect(self.vault, mode="daily", until="2099-01-30")
        self.assertEqual(older["updates"], [])
        ledger.write_text(ledger.read_text() + rows[-1])
        newer = rc.collect(self.vault, mode="daily", until="2099-01-30")
        rd.write(self.vault, "NEWER STATUS", newer)
        target = self.vault / "inbox/digest/2099-01-30-daily-digest.html"
        state = self.vault / rc.DIGEST_UPDATES_STATE
        before = state.read_bytes()
        fresh = rc.collect(self.vault, mode="daily", until="2099-01-30")
        for manifest in (older, fresh):
            with self.assertRaisesRegex(SystemExit, "existing digest"):
                rd.write(self.vault, "NO STATUS UPDATE", manifest)
            self.assertEqual(target.read_text(), "NEWER STATUS")
            self.assertEqual(state.read_bytes(), before)
        modified = target.stat().st_mtime_ns
        self.assertEqual(rd.write(self.vault, "NEWER STATUS", newer), 0)
        self.assertEqual(target.stat().st_mtime_ns, modified)

    def test_unreadable_update_configuration_blocks_publication(self):
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-30")
        rd.write(self.vault, "CURRENT", manifest)
        state = self.vault / rc.DIGEST_UPDATES_STATE
        before = state.read_bytes()
        config = self.vault / rc.DIGEST_UPDATES_CONFIG
        target = self.vault / "inbox/digest/historical.html"
        for updates in (manifest["updates"], []):
            with self.subTest(updates=bool(updates)):
                config.write_text("[broken")
                candidate = {**manifest, "updates": updates}
                with self.assertRaisesRegex(SystemExit, "config unreadable"):
                    rd.write(self.vault, "HISTORICAL", candidate, out=target)
                self.assertFalse(target.exists())
                self.assertEqual(state.read_bytes(), before)

    def test_concurrent_publications_keep_both_delivery_histories(self):
        older = rc.collect(self.vault, mode="daily", until="2099-01-25")
        newer = rc.collect(self.vault, mode="daily", until="2099-01-30")
        first_read, release, second_read, first_done = Event(), Event(), Event(), Event()
        original = rc.load_update_state
        reads = []
        def read(ov):
            value = original(ov)
            reads.append(None)
            if len(reads) == 1:
                first_read.set()
                self.assertTrue(release.wait(5))
            else:
                second_read.set()
                self.assertTrue(first_done.wait(5))
            return value
        def first():
            try:
                return rd.write(self.vault, "FIRST", older)
            finally:
                first_done.set()
        with patch.object(rc, "load_update_state", side_effect=read), ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first)
            self.assertTrue(first_read.wait(5))
            two = pool.submit(rd.write, self.vault, "SECOND", newer)
            second_read.wait(0.2)
            release.set()
            self.assertEqual((one.result(timeout=10), two.result(timeout=10)), (0, 0))
        state = json.loads((self.vault / rc.DIGEST_UPDATES_STATE).read_text())
        self.assertIn("finance/signals/2099-01-25-monitor.md", state["delivered"])
        self.assertIn("inbox/feed/2099-01-30-feed.md", state["delivered"])
        replay = rc.collect(self.vault, mode="daily", until="2099-01-31")
        self.assertEqual(replay["updates"], [])
        self.assertEqual(replay["carry"]["files"], 0)

    def test_unreadable_output_never_becomes_delivered_or_acknowledgeable(self):
        path = self.vault / "inbox/feed/2099-01-30-feed.md"
        path.chmod(0)
        try:
            for kwargs in ({"until": "2099-01-30"}, {"until": "2099-01-31"}, {"unacked": True}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(SystemExit, "routine output unreadable"):
                    rc.collect(self.vault, mode="daily", **kwargs)
        finally:
            path.chmod(0o600)
        self.assertFalse((self.vault / rc.DIGEST_UPDATES_STATE).exists())
        self.assertFalse((self.vault / "_meta/routine_acks.json").exists())
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-31")
        self.assertIn(path.name, [s["name"] for _, s in core.iter_sources(manifest)])

    def test_health_uses_the_requested_dates_local_offset(self):
        import routine_status
        routine = next(r for r in core.load_routines(self.vault) if r.include)
        routine.execution = "local"
        try:
            with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                time.tzset()
                for day, offset in (("2026-01-30", "-08:00"), ("2026-07-30", "-07:00")):
                    runs = [{"routine": routine.name, "expected_start_time": datetime.fromisoformat(f"{day}T{hour}{offset}"),
                             "state": state, "state_name": state.title()} for hour, state in (
                        ("12:00:00", "COMPLETED"), ("23:30:00", "FAILED"))]
                    with patch.object(routine_status, "recent_runs", return_value=runs) as recent:
                        health = rc.collect_health(self.vault, [routine], {}, date.fromisoformat(day), date.fromisoformat(day))
                    self.assertEqual(recent.call_args.args[0].utcoffset(), timedelta(hours=int(offset[:3])))
                    self.assertEqual((health["completed"], health["failed"]), (1, 1))
        finally:
            time.tzset()

    def test_reformatting_the_ledger_does_not_replay_delivered_rows(self):
        # The cursor is a hash of normalized values, so pipe alignment and cell
        # padding must not make it miss and replay the whole source.
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-31", days=1)
        self.assertEqual(len(manifest["updates"]), 2)
        rd.write(self.vault, rr.render(manifest), manifest, routine_name="digest-writer")

        ledger = self.vault / "personal" / "status-tracker.md"
        reformatted = "\n".join(
            "|  " + "  |  ".join(cell.strip() for cell in line.strip("|").split("|")) + "  |"
            if line.startswith("|") and "---" not in line
            else line
            for line in ledger.read_text(encoding="utf-8").splitlines()
        )
        ledger.write_text(reformatted + "\n", encoding="utf-8")

        replay = rc.collect(self.vault, mode="daily", until="2099-02-01")
        self.assertEqual(replay["updates"], [])
        self.assertEqual(
            [w for w in replay["update_warnings"] if "no longer matches" in w], []
        )

    def test_a_cursor_written_before_normalization_still_resolves(self):
        # Dual read: no $OV state migration is required for existing cursors.
        last_raw = "| 2099-02 | 2099-01-30 | 2098-07-01 | No | Keep monitoring | [Primary](https://example.com/status-2) |"
        (self.vault / rc.DIGEST_UPDATES_STATE).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "daily": {"status-ledger": rc.legacy_row_id("status-ledger", last_raw)},
                    "delivered": {},
                }
            ),
            encoding="utf-8",
        )
        resumed = rc.collect(self.vault, mode="daily", until="2099-02-01")
        self.assertEqual(resumed["updates"], [])
        self.assertEqual(
            [w for w in resumed["update_warnings"] if "no longer matches" in w], []
        )

    def test_malformed_ledger_rows_are_reported_not_dropped_silently(self):
        ledger = self.vault / "personal" / "status-tracker.md"
        ledger.write_text(
            ledger.read_text(encoding="utf-8") + "| 2099-03 | 2099-01-30 |\n",
            encoding="utf-8",
        )
        manifest = rc.collect(self.vault, mode="weekly", until="2099-01-30")
        self.assertTrue(
            any("cell count disagrees" in w for w in manifest["update_warnings"]),
            manifest["update_warnings"],
        )

    def test_weekly_update_window_is_independent_of_daily_delivery(self):
        daily = rc.collect(self.vault, mode="daily", until="2099-01-31")
        rd.write(self.vault, rr.render(daily), daily, routine_name="digest-writer")

        weekly = rc.collect(self.vault, mode="weekly", until="2099-01-31")
        self.assertEqual(len(weekly["updates"]), 2)

    def test_backdated_daily_does_not_pull_a_future_update(self):
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-29")
        self.assertEqual(manifest["updates"], [])

    def test_empty_window_is_not_an_error(self):
        manifest = rc.collect(self.vault, mode="weekly", until="2020-01-01")
        self.assertEqual(manifest["counts"]["files"], 0)
        self.assertEqual(manifest["lanes"], [])

    def test_max_files_truncates_and_flags_it(self):
        manifest = rc.collect(self.vault, mode="weekly", until="2099-01-30", max_files=2)
        self.assertTrue(manifest["truncated"])
        self.assertEqual(manifest["counts"]["files"], 2)

    def test_daily_carries_yesterdays_undelivered_file_and_says_so(self):
        """A routine that finishes after the morning run writes a file dated
        today; a strict one-day window tomorrow would never see it."""
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-31")
        sources = [s for _, s in core.iter_sources(manifest)]
        self.assertEqual([s["name"] for s in sources], ["2099-01-30-feed.md"])
        self.assertTrue(sources[0]["carried"])
        self.assertEqual(
            manifest["carry"], {"days": rc.DAILY_CARRY_DAYS, "files": 1, "already_delivered": 0}
        )
        # The reach is one day, not a backlog: the 01-28 scan is not carried
        # into 01-30, and today's own files are never marked carried.
        today = rc.collect(self.vault, mode="daily", until="2099-01-30")
        names = [s["name"] for _, s in core.iter_sources(today)]
        self.assertEqual(names, ["2099-01-30-feed.md"])
        self.assertNotIn("carried", next(s for _, s in core.iter_sources(today)))
        self.assertEqual(today["carry"]["files"], 0)

    def test_delivered_file_is_not_carried_again_but_a_same_day_rerun_repeats(self):
        first = rc.collect(self.vault, mode="daily", until="2099-01-30")
        rd.write(self.vault, rr.render(first), first, routine_name="digest-writer")
        state = json.loads(
            (self.vault / rc.DIGEST_UPDATES_STATE).read_text(encoding="utf-8")
        )
        self.assertEqual(state["delivered"]["inbox/feed/2099-01-30-feed.md"], "2099-01-30")

        rerun = rc.collect(self.vault, mode="daily", until="2099-01-30")
        self.assertEqual([s["name"] for _, s in core.iter_sources(rerun)], ["2099-01-30-feed.md"])

        tomorrow = rc.collect(self.vault, mode="daily", until="2099-01-31")
        self.assertEqual(tomorrow["counts"]["files"], 0)
        self.assertEqual(tomorrow["carry"], {"days": 1, "files": 0, "already_delivered": 1})

    def test_delivered_ledger_is_pruned_and_keeps_the_first_date(self):
        state_path = self.vault / rc.DIGEST_UPDATES_STATE
        state_path.write_text(
            json.dumps({"schema": 1, "daily": {}, "delivered": {"old/file.md": "2098-12-01"}}),
            encoding="utf-8",
        )
        first = rc.collect(self.vault, mode="daily", until="2099-01-30")
        rd.write(self.vault, rr.render(first), first, routine_name="digest-writer")
        later = rc.collect(self.vault, mode="daily", until="2099-01-31")
        rd.write(self.vault, rr.render(later), later, routine_name="digest-writer")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("old/file.md", state["delivered"])
        self.assertEqual(state["delivered"]["inbox/feed/2099-01-30-feed.md"], "2099-01-30")

    def test_explicit_window_flags_disable_the_carry(self):
        by_days = rc.collect(self.vault, mode="daily", until="2099-01-31", days=1)
        self.assertEqual(by_days["counts"]["files"], 0)
        self.assertNotIn("carry", by_days)
        by_since = rc.collect(self.vault, mode="daily", since="2099-01-31", until="2099-01-31")
        self.assertNotIn("carry", by_since)
        weekly = rc.collect(self.vault, mode="weekly", until="2099-01-31")
        self.assertNotIn("carry", weekly)


class ContextSourceTests(VaultCase):
    def _declare(self, extra: str = '') -> None:
        watch = self.vault / "_tools/routines/registry.toml"
        watch.write_text(WATCH_TOML + '\n[[routine]]\nname = "sweep"\nlabel = "Lab sweep"\n'
                         'output_dir = "research/sweeps"\nfile_pattern = "*.md"\n'
                         'digest = { context = "frontier_labs", lane = "Research" }\n' + extra,
                         encoding="utf-8")
        (self.vault / "research/sweeps").mkdir(parents=True, exist_ok=True)

    def test_stale_context_is_independent_of_window_carry_acks_and_file_cap(self):
        self._declare()
        path = self.vault / "research/sweeps/2099-01-20-sweep.md"
        path.write_text("# Old sweep\n\nBackground only.", encoding="utf-8")
        (self.vault / "_meta/routine_acks.json").write_text(
            json.dumps({"research/sweeps": "2099-01-20-sweep.md"}), encoding="utf-8")
        cases = ({"until": "2099-02-10"}, {"until": "2099-01-30", "max_files": 0},
                 {"until": "2099-02-10", "unacked": True, "max_files": 0})
        for options in cases:
            with self.subTest(options=options):
                manifest = rc.collect(self.vault, mode="daily", **options)
                ref = manifest["context_sources"]["frontier_labs"]
                self.assertEqual(ref["path"], "research/sweeps/2099-01-20-sweep.md")
                self.assertEqual(ref["date_source"], "filename")
                self.assertEqual(manifest["context_warnings"], [])
                self.assertEqual(manifest["counts"]["files"], 0)
                self.assertEqual(manifest["counts"]["bytes"], 0)
                self.assertEqual(manifest["acks"], {})
                self.assertEqual(list(core.iter_sources(manifest)), [])
                self.assertEqual(core.manifest_names_by_dir(manifest), {})
                self.assertIsNone(core.deep_read_lane_gap(
                    {"entries": [{"lane": "Finance", "title": "A pick"}]}, manifest))

    def test_context_uses_dates_not_filename_order_and_never_reads_bodies(self):
        self._declare()
        directory = self.vault / "research/sweeps"
        for name in ("z_2099-01-10.md", "a_2099-01-25.md", "future_2099-02-01.md"):
            (directory / name).write_text("secret body must not be read", encoding="utf-8")
        routines = core.load_routines(self.vault)
        with patch.object(Path, "read_text", side_effect=AssertionError("metadata only")):
            contexts, warnings = rc.collect_context_sources(self.vault, routines, date(2099, 1, 30))
        ref = contexts["frontier_labs"]
        self.assertEqual(ref["name"], "a_2099-01-25.md")
        self.assertEqual(ref["date"], "2099-01-25")
        self.assertEqual(set(ref), {"routine", "label", "path", "name", "date", "date_source", "bytes", "anchor"})
        self.assertEqual(warnings, [])

    def test_context_does_not_consume_a_fresh_file_slot(self):
        self._declare()
        (self.vault / "research/sweeps/2099-01-20.md").write_text("background", encoding="utf-8")
        manifest = rc.collect(self.vault, mode="daily", days=1, until="2099-01-30", max_files=1)
        self.assertEqual(manifest["counts"]["files"], 1)
        self.assertEqual([s["routine"] for _, s in core.iter_sources(manifest)], ["feed-digest"])
        self.assertEqual(manifest["context_sources"]["frontier_labs"]["name"], "2099-01-20.md")
        self.assertNotIn("research/sweeps", manifest["acks"])

    def test_context_reports_mtime_fallback_and_accepts_contained_symlinks(self):
        self._declare()
        directory = self.vault / "research/sweeps"
        source = directory / "undated.md"
        source.write_text("metadata only", encoding="utf-8")
        stamp = datetime(2099, 1, 22, 12).timestamp()
        os.utime(source, (stamp, stamp))
        contexts, warnings = rc.collect_context_sources(self.vault, core.load_routines(self.vault), date(2099, 1, 30))
        self.assertEqual(contexts["frontier_labs"]["date_source"], "mtime")
        self.assertEqual(contexts["frontier_labs"]["date"], "2099-01-22")
        self.assertEqual(warnings, [])
        (directory / "2099-01-23.md").symlink_to(source)
        contexts, warnings = rc.collect_context_sources(self.vault, core.load_routines(self.vault), date(2099, 1, 30))
        self.assertEqual(contexts["frontier_labs"]["date"], "2099-01-23")
        self.assertEqual(contexts["frontier_labs"]["date_source"], "filename")
        self.assertEqual(warnings, [])

    def test_context_missing_future_and_unsafe_sources_warn(self):
        self._declare()
        routines = core.load_routines(self.vault)
        context_routine = next(r for r in routines if r.context)
        directory = self.vault / "research/sweeps"
        contexts, warnings = rc.collect_context_sources(self.vault, routines, date(2099, 1, 30))
        self.assertEqual(contexts, {})
        self.assertTrue(any("no eligible source" in warning for warning in warnings))
        (directory / "2099-02-01.md").write_text("future", encoding="utf-8")
        outside = Path(self.tmp.name) / "2099-01-29.md"
        outside.write_text("outside", encoding="utf-8")
        (directory / outside.name).symlink_to(outside)
        inside_other_dir = self.vault / "research/2099-01-28.md"
        inside_other_dir.write_text("outside declared directory", encoding="utf-8")
        (directory / inside_other_dir.name).symlink_to(inside_other_dir)
        with patch.object(Path, "read_text", side_effect=AssertionError("metadata only")):
            contexts, warnings = rc.collect_context_sources(self.vault, routines, date(2099, 1, 30))
        self.assertEqual(contexts, {})
        self.assertTrue(any("unsafe" in warning for warning in warnings))
        for output_dir, pattern in (("../outside", "*.md"), ("research/sweeps", "../*.md"),
                                    ("research/missing", "*.md")):
            context_routine.output_dir, context_routine.file_pattern = output_dir, pattern
            contexts, warnings = rc.collect_context_sources(self.vault, routines, date(2099, 1, 30))
            self.assertEqual(contexts, {})
            self.assertTrue(warnings)
        (self.vault / "research/escape").symlink_to(Path(self.tmp.name), target_is_directory=True)
        context_routine.output_dir, context_routine.file_pattern = "research/escape", "*.md"
        contexts, warnings = rc.collect_context_sources(self.vault, routines, date(2099, 1, 30))
        self.assertEqual(contexts, {})
        self.assertTrue(any("outside vault" in warning for warning in warnings))

    def test_context_declarations_validate_keys_uniqueness_and_count(self):
        watch = self.vault / "_tools/routines/registry.toml"
        for value in ('"../escape"', '""', '"UPPER"', '7'):
            watch.write_text(WATCH_TOML.replace('file_pattern = "*-feed.md"',
                'file_pattern = "*-feed.md"\ndigest = { context = ' + value + ' }'), encoding="utf-8")
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "digest.context"):
                core.load_routines(self.vault)
        self._declare()
        watch.write_text(watch.read_text().replace('file_pattern = "*-feed.md"',
            'file_pattern = "*-feed.md"\ndigest = { context = "frontier_labs" }'), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "duplicate digest.context"):
            core.load_routines(self.vault)
        rows = ''.join(f'\n[[routine]]\nname = "context-{i}"\noutput_dir = "research/{i}"\n'
                       f'digest = {{ context = "context_{i}" }}\n' for i in range(core.MAX_CONTEXT_SOURCES + 1))
        watch.write_text(WATCH_TOML + rows, encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "at most"):
            core.load_routines(self.vault)

    def test_background_index_citations_and_warning_escaping(self):
        self._declare()
        (self.vault / "research/sweeps/2099-01-20.md").write_text("background", encoding="utf-8")
        manifest = rc.collect(self.vault, mode="daily", until="2099-02-10")
        ref = manifest["context_sources"]["frontier_labs"]
        ref["label"] = "<script>background</script>"
        manifest["context_warnings"] = ['<img src=x onerror=alert(1)> missing source']
        overview = {"schema": 1, "sections": [{"title": "Signal", "bullets": [
            {"text": "A cited background fact", "sources": [ref["path"]]}]}]}
        document = rr.render(manifest, overview)
        parsed = ParsedDocument(document)
        self.assertIn("Background context", parsed.text)
        self.assertNotIn("unmatched", parsed.text)
        self.assertFalse(parsed.root.find_all("script"))
        self.assertFalse(parsed.root.find_all("img"))
        self.assertEqual(document.count('id="' + ref["anchor"] + '"'), 1)
        self.assertGreater(parsed.text.index("missing source"), parsed.text.index("以上 "))
        manifest["context_sources"]["duplicate"] = dict(ref)
        self.assertEqual(rr.render(manifest, overview).count('id="' + ref["anchor"] + '"'), 1)
        manifest["lanes"] = [{"lane": "Research", "files": 1, "sources": [dict(ref)]}]
        document = rr.render(manifest, overview)
        self.assertEqual(document.count('id="' + ref["anchor"] + '"'), 1)
        self.assertNotIn("Background context", ParsedDocument(document).text)


class RenderTests(VaultCase):
    def test_title_shapes(self):
        self.assertEqual(rr.digest_title(self.manifest), "Atelier Weekly — 2099-01-24 → 2099-01-30")
        daily = rc.collect(self.vault, mode="daily", until="2099-01-30")
        self.assertEqual(rr.digest_title(daily), "Atelier Daily — 2099-01-30")
        backlog = rc.collect(self.vault, mode="weekly", until="2099-01-30", unacked=True)
        self.assertIn("backlog", rr.digest_title(backlog))

    def test_carried_source_is_labelled_in_the_index(self):
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-31")
        html = rr.render(manifest)
        self.assertIn("补录", html)
        self.assertNotIn("补录", rr.render(self.manifest))

    def test_gaps_render_in_the_colophon_never_as_a_section(self):
        overview = {"schema": 1, "headline": "h", "sections": [], "gaps": ["Readwise CLI unavailable"]}
        html = rr.render(self.manifest, overview)
        fold = html.index("以上 ")
        self.assertGreater(html.index("Readwise CLI unavailable"), fold)
        self.assertGreater(html.index("输入缺口"), fold)
        self.assertNotRegex(html, r"<h2\b[^>]*>输入缺口")
        quiet = {**self.manifest, "lanes": [lane for lane in self.manifest["lanes"] if lane["lane"] != "Tech feed"]}
        self.assertNotIn("输入缺口", rr.render(quiet, {"schema": 1, "headline": "h", "sections": []}))

    def test_brief_renders_above_the_overview(self):
        """The action surface is the first screen, ahead of the intel overview."""
        brief = self._brief(
            {"text": "Hotel credit 明天", "source": "finance/example-tracker.md:107"},
            kind="closing_now", heading="今天/明天关窗 1 件",
        )
        brief["warnings"] = ["deadline index stale 12d"]
        overview = {"schema": 1, "sections": [{"title": "情报", "bullets": [{"text": "x"}]}]}
        document = rr.render(self.manifest, overview, brief)
        self.assertIn("今天/明天关窗 1 件", document)
        self.assertIn("Hotel credit 明天", document)
        self.assertIn("deadline index stale 12d", document)
        self.assertLess(document.index("今天/明天关窗"), document.index("情报"))
        self.assertLess(document.index("情报"), document.index("Source index"))

    def test_optional_inputs_keep_source_navigation_and_exclusion_disclosure(self):
        document = rr.render(self.manifest)
        self.assertNotIn("今日 <span", document)
        self.assertIn("No overview supplied", document)
        self.assertIn("Source index", document)
        self.assertIn("daily feed digest", document)
        self.assertIn('href="https://example.com/one"', document)
        self.assertRegex(document, r'<code[^>]*>inbox/feed/2099-01-30-feed\.md</code>')
        self.assertIn("Excluded from this digest", document)
        self.assertIn("maintenance output", document)

    def test_configured_updates_render_before_the_overview(self):
        overview = {"schema": 1, "sections": [{"title": "情报", "bullets": [{"text": "x"}]}]}
        document = rr.render(self.manifest, overview)
        self.assertIn("状态更新", document)
        self.assertIn("Status ledger", document)
        self.assertIn("2098-06-01", document)
        self.assertIn('href="https://example.com/status"', document)
        self.assertLess(document.index("状态更新"), document.index("情报"))

    def test_folded_brief_group_renders_heading_only(self):
        brief = self._brief(tier=3, kind="recurring", heading="recurring: 9 条逾期", folded=True)
        parsed = ParsedDocument(rr.render(self.manifest, None, brief))
        groups = parsed.root.find_all("td", class_name="ledger-group")
        self.assertEqual([group.text.strip() for group in groups], ["recurring: 9 条逾期"])
        self.assertEqual(parsed.root.find_all("tr", class_name="ledger-row"), [])

    def test_unknown_input_schemas_are_rejected(self):
        path = Path(self.tmp.name) / "unknown.json"
        path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
        for loader in (rc.load_brief, rc.load_overview, rc.load_context):
            with self.subTest(loader=loader.__name__), self.assertRaises(SystemExit):
                loader(path)

    def test_brief_text_is_escaped(self):
        brief = self._brief({"text": "<img onerror=x>"}, kind="closing_now", heading="<script>h</script>")
        brief["warnings"] = ["<b>w</b>"]
        document = rr.render(self.manifest, None, brief)
        parsed = ParsedDocument(document)
        self.assertEqual(parsed.root.find_all("script"), [])
        self.assertEqual(parsed.root.find_all("img"), [])
        self.assertIn("<script>h</script>", parsed.text)
        self.assertIn("<img onerror=x>", parsed.text)

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

    def test_overview_has_real_links_and_mail_safe_provenance_labels(self):
        source_path = "inbox/feed/2099-01-30-feed.md"
        document = rr.render(self.manifest, self._overview_with_source(source_path))
        self.assertIn("<strong>bold</strong>", document)
        self.assertIn('href="https://example.com/z"', document)
        # The index entry keeps its id for the browser case.
        self.assertIn(f'id="{core.source_anchor(source_path)}"', document)

        # Gmail rewrites #anchor links; labels remain useful in both hosts.
        self.assertNotIn(f'href="#{core.source_anchor(source_path)}"', document)
        self.assertRegex(document, r'<code[^>]*>2099-01-30-feed\.md</code>')

    def test_a_source_outside_the_manifest_is_marked_unmatched(self):
        document = rr.render(
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
        document = rr.render(self.manifest, overview)
        parsed = ParsedDocument(document)
        self.assertEqual(parsed.root.find_all("script"), [])
        self.assertEqual(parsed.root.find_all("b"), [])
        self.assertIn("<script>alert(1)</script> and <b>x</b>", parsed.text)


class RepresentativeDigestTests(unittest.TestCase):
    """One shareable fixture pins the whole rendered information hierarchy."""

    def _render(self, mode: str) -> tuple[str, ParsedDocument]:
        document = rr.render(*representative_digest_inputs(mode))
        return document, ParsedDocument(document)

    def assert_text_order(self, text: str, *needles: str) -> None:
        offsets = [text.index(needle) for needle in needles]
        self.assertEqual(offsets, sorted(offsets), needles)

    def test_daily_fixture_preserves_content_and_visual_order(self):
        document, parsed = self._render("daily")
        text = parsed.text
        self.assert_text_order(
            text,
            "Atelier Daily",
            "需要开始处理 1 件",
            "本季主线",
            "TODO 到期 1 件",
            "状态更新",
            "需要的决策",
            "前沿实验室",
            "routine 摘要",
            "新文章",
            "以上 ",
            "情报详读",
            "随机回顾",
            "Source index",
            "输入缺口",
            "Generated by",
        )
        for visible in (
            "synthetic context warning",
            "synthetic deadline index is stale",
            "example status ledger is one day stale",
            "Example credit",
            "Ship the example milestone",
            "Review the example",
            "This incomplete decision must become a signal.",
            "Example Lab",
            "A useful saved article",
            "第一条可复核事实。",
            "第二条可复核事实。",
            "示例模型发布了可复核的更新。",
            "An earlier example",
            "补录",
            "Synthetic optional source was unavailable.",
        ):
            self.assertIn(visible, text)
        for hidden in (
            "A bare title that must be dropped",
            "第三条应被深度上限裁掉。",
            "第五行不应出现",
            "An unreviewed item that must stay hidden",
        ):
            self.assertNotIn(hidden, text)
        self.assertEqual(len(parsed.root.find_all("li", class_name="decision")), 1)
        self.assertEqual(len(parsed.root.find_all(class_name="decision-option")), 2)
        self.assertIn("https://example.com/research", [node.attrs.get("href") for node in parsed.root.find_all("a")])
        self.assertEqual(rr.check_html(document), [])

    def test_weekly_fixture_removes_daily_actions_but_keeps_intel_order(self):
        _document, parsed = self._render("weekly")
        text = parsed.text
        self.assertIn("Atelier Weekly", text)
        for daily_only in (
            "需要开始处理 1 件",
            "本季主线",
            "TODO 到期 1 件",
            "需要的决策",
            "A useful saved article",
        ):
            self.assertNotIn(daily_only, text)
        self.assert_text_order(
            text,
            "状态更新",
            "信号",
            "前沿实验室",
            "routine 摘要",
            "以上 ",
            "情报详读",
            "随机回顾",
            "Source index",
        )
        self.assertEqual(parsed.root.find_all("li", class_name="decision"), [])


class MjmlBridgeTests(unittest.TestCase):
    """The local compiler is a fail-closed, offline boundary for trusted trees."""

    @staticmethod
    def _container(tag: str, *children: dict, **attributes: str) -> dict:
        return {"tagName": tag, "attributes": attributes, "children": list(children)}

    @staticmethod
    def _content(tag: str, content: str = "", **attributes: str) -> dict:
        return {"tagName": tag, "attributes": attributes, "content": content}

    @classmethod
    def _tree(cls, content: str = "<p>safe</p>") -> dict:
        column = cls._container("mj-column", cls._content("mj-text", content))
        return cls._container(
            "mjml", cls._container("mj-body", cls._container("mj-section", column)), lang="en"
        )

    @staticmethod
    def _leaf(tree: dict) -> dict:
        node = tree
        while node.get("children"):
            node = node["children"][-1]
        return node

    @staticmethod
    def _bridge_env() -> dict[str, str]:
        from _node import SYSTEM_PATH

        return {
            "PATH": SYSTEM_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NODE_ENV": "production",
            "MJML_BROWSER": "1",
        }

    @staticmethod
    def _process(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["node"], returncode, stdout=stdout, stderr=stderr)

    def _run_bridge(
        self, tree: dict, *, cwd: Path = REPO_ROOT, browser_guard: bool = True
    ) -> subprocess.CompletedProcess[str]:
        from _node import node_executable

        env = self._bridge_env()
        if not browser_guard:
            env.pop("MJML_BROWSER")
        return subprocess.run(
            [str(node_executable()), str(rr._MJML_BRIDGE)],
            input=json.dumps(tree, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=cwd,
            env=env,
            timeout=rr.MJML_TIMEOUT_SECONDS,
            check=False,
        )

    def test_real_local_compile_returns_a_complete_html_document(self):
        document = rr._compile_mjml(self._tree("<p>compiled marker</p>"))
        parsed = ParsedDocument(document)
        self.assertTrue(document.lower().startswith("<!doctype html"))
        self.assertEqual(len(parsed.root.find_all("html")), 1)
        self.assertEqual(len(parsed.root.find_all("head")), 1)
        self.assertEqual(len(parsed.root.find_all("body")), 1)
        self.assertIn("compiled marker", parsed.text)
        self.assertNotIn("<mjml", document.lower())

    def test_python_bridge_uses_only_the_pinned_absolute_process_contract(self):
        import _node

        tree = self._tree()
        completed = self._process(
            stdout="<!doctype html><html><head></head><body>safe</body></html>"
        )
        with (
            patch.object(_node, "node_executable", return_value=Path("/system/node")),
            patch.object(_node.subprocess, "run", return_value=completed) as run,
        ):
            document = rr._compile_mjml(tree)
        self.assertTrue(document.endswith("\n"))
        command = ["/system/node", str(rr._MJML_BRIDGE)]
        run.assert_called_once_with(
            command,
            input=json.dumps(tree, ensure_ascii=False, separators=(",", ":")),
            text=True,
            capture_output=True,
            cwd=REPO_ROOT,
            env=self._bridge_env(),
            timeout=rr.MJML_TIMEOUT_SECONDS,
            check=False,
        )
        self.assertTrue(all(Path(arg).is_absolute() for arg in command))
        self.assertLessEqual(rr.MJML_TIMEOUT_SECONDS, 30)

    def test_missing_dependency_timeout_bad_exit_and_incomplete_output_fail_closed(self):
        import _node

        failures = (
            (OSError("node missing"), "unavailable"),
            (subprocess.TimeoutExpired(["node"], 1), "unavailable"),
            (self._process(1, stderr="ERR_MODULE_NOT_FOUND: mjml"), "ERR_MODULE_NOT_FOUND"),
            (self._process(stdout="partial"), "incomplete"),
        )
        for outcome, message in failures:
            with self.subTest(outcome=type(outcome).__name__):
                with (
                    patch.object(_node, "node_executable", return_value=Path("/system/node")),
                    patch.object(_node.subprocess, "run", side_effect=[outcome]),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    rr._compile_mjml(self._tree())

        with (
            patch.object(_node.subprocess, "run") as run,
            self.assertRaisesRegex(RuntimeError, "input exceeds"),
        ):
            rr._compile_mjml(self._tree("x" * rr.MJML_INPUT_BYTES))
        run.assert_not_called()

    def test_browser_guard_blocks_cwd_mjml_config_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            marker = "REVIEW_CONFIG_COMPONENT_EXECUTED"
            (cwd / ".mjmlconfig").write_text(
                json.dumps({"packages": ["./config-probe.cjs"]}), encoding="utf-8"
            )
            (cwd / "config-probe.cjs").write_text(
                f"process.stderr.write('{marker}\\n'); module.exports = {{}};\n",
                encoding="utf-8",
            )
            exposed = self._run_bridge(self._tree(), cwd=cwd, browser_guard=False)
            guarded = self._run_bridge(self._tree(), cwd=cwd)
        self.assertEqual(exposed.returncode, 0, exposed.stderr)
        self.assertIn(marker, exposed.stderr)
        self.assertEqual(guarded.returncode, 0, guarded.stderr)
        self.assertNotIn(marker, guarded.stderr)

    def test_bridge_rejects_active_html_css_and_component_injection(self):
        content_cases = (
            '<a href=javascript:alert(1)>link</a>',
            '<svg/onload=alert(1)></svg>',
            '<img src="https://example.invalid/pixel">',
            '<script>alert(1)</script>',
            '<span style="background:u\\72l(https://example.invalid/pixel)">x</span>',
            '<mj-include path="/etc/passwd"></mj-include>',
            '<!--[if mso]><img src="https://example.invalid/pixel"><![endif]-->safe',
        )
        tree_cases = [(self._tree(content), "unsafe") for content in content_cases]
        for tag in ("mj-include", "mj-raw", "script", "img"):
            tree = self._tree()
            self._leaf(tree)["tagName"] = tag
            tree_cases.append((tree, "invalid"))
        css_class = self._tree()
        self._leaf(css_class)["attributes"] = {"css-class": 'safe" onmouseover="alert(1)'}
        tree_cases.append((css_class, "invalid"))
        font = self._tree()
        font["children"].insert(
            0,
            self._container(
                "mj-head",
                self._container(
                    "mj-attributes",
                    self._content(
                        "mj-all",
                        **{"font-family": "serif; background:u\\72l(https://example.invalid/pixel)"},
                    ),
                ),
            ),
        )
        tree_cases.append((font, "invalid"))
        for tree, rejection in tree_cases:
            with self.subTest(tree=tree):
                result = self._run_bridge(tree)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(rejection, result.stderr.lower())

    def test_security_words_remain_safe_when_they_are_only_prose(self):
        manifest = representative_digest_inputs()[0]
        prose = (
            "Discuss javascript: links, file: paths, onerror= handlers, "
            "<svg/onload=alert(1)>, and <mj-include> as inert prose."
        )
        overview = {"schema": 1, "sections": [{"title": "Safety", "bullets": [{"text": prose}]}]}
        parsed = ParsedDocument(rr.render(manifest, overview))
        self.assertIn(prose, parsed.text)
        self.assertEqual(parsed.root.find_all("svg"), [])
        self.assertEqual(parsed.root.find_all("mj-include"), [])
        self.assertFalse(
            any(
                name.lower().startswith("on")
                for node in parsed.root.find_all()
                for name in node.attrs
            )
        )


class WriteTests(VaultCase):
    def test_overdue_todos_stop_after_three_daily_artifacts(self):
        import daily_brief as db
        import todos

        todo = todos.Todo("Example due:2099-01-01", str(self.vault / "gtd/tasks.md"), 1,
                          "open", due="2099-01-01")
        state = self.vault / rc.DIGEST_UPDATES_STATE
        with patch.object(todos, "collect_open_todos", return_value=[todo]):
            for number in range(1, 5):
                today = date(2099, 1, 20 + number)
                groups = db.load_todos(self.vault, today, [])
                if number == 4:
                    self.assertEqual(groups, [])
                    break
                self.assertEqual(len(groups[0].items), 1)
                item = vars(groups[0].items[0])
                brief = self._brief(item, kind="todo_now")
                brief["date"] = today.isoformat()
                manifest = {"mode": "daily", "window": {"until": today.isoformat()}}
                out = self.vault / f"day-{number}.html"
                before = state.read_bytes() if state.exists() else None
                rd.write(self.vault, "digest", manifest, out=out, brief=brief, dry_run=True)
                self.assertEqual(state.read_bytes() if state.exists() else None, before)
                with patch.object(rd, "atomic_write", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        rd.write(self.vault, "digest", manifest, out=out, brief=brief)
                self.assertEqual(state.read_bytes() if state.exists() else None, before)
                for _ in range(2):
                    rd.write(self.vault, "digest", manifest, out=out, brief=brief)
                shown = rc.load_todo_reminders(self.vault)[item["reminder_id"]]
                self.assertEqual(len(shown), number)
                todo.line += 1
                self.assertEqual(db.load_todos(self.vault, today, [])[0].items[0].reminder_id,
                                 item["reminder_id"])
            # A previously collected brief cannot bypass the limit on a later day.
            brief["date"] = manifest["window"]["until"] = "2099-01-24"
            with self.assertRaisesRegex(SystemExit, "limit reached"):
                rd.write(self.vault, "digest", manifest, out=self.vault / "stale.html", brief=brief)
            self.assertFalse((self.vault / "stale.html").exists())
            rd.write(self.vault, "weekly", self.manifest, out=self.vault / "weekly.html", brief=brief)
            self.assertEqual(len(rc.load_todo_reminders(self.vault)[item["reminder_id"]]), 3)
            todo.due, todo.text = "2099-01-23", "Example due:2099-01-23"
            self.assertEqual(len(db.load_todos(self.vault, date(2099, 1, 24), [])[0].items), 1)
            self.assertEqual(todo.state, "open")

    def test_unreadable_reminder_state_warns_instead_of_blanking_the_brief(self):
        import daily_brief as db
        import todos

        todo = todos.Todo("Example due:2099-01-01", str(self.vault / "gtd/tasks.md"), 1,
                          "open", due="2099-01-01")
        state = self.vault / rc.DIGEST_UPDATES_STATE
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"schema": 99}', encoding="utf-8")
        warnings: list[str] = []
        with patch.object(todos, "collect_open_todos", return_value=[todo]):
            groups = db.load_todos(self.vault, date(2099, 1, 21), warnings)
        self.assertEqual(len(groups[0].items), 1)
        self.assertTrue(any("reminder state ignored" in w for w in warnings), warnings)

    def test_explicit_and_inferred_routines_land_in_the_declared_directory(self):
        written = self.vault / "inbox" / "digest" / "2099-01-30-weekly-digest.html"
        for name, routine, html in (
            ("explicit", "digest-writer", "<h1>same artifact</h1>"),
            ("unique inference", None, "<h1>same artifact</h1>"),
        ):
            with self.subTest(case=name):
                kwargs = {} if routine is None else {"routine_name": routine}
                self.assertEqual(rd.write(self.vault, html, self.manifest, **kwargs), 0)
                self.assertEqual(written.read_text(encoding="utf-8"), html)

    def test_writer_refusals(self):
        registry = self.vault / "_tools/routines/registry.toml"
        original = registry.read_text(encoding="utf-8")
        cases = (
            ("self ingestion", original, "feed-digest", ("not excluded",)),
            ("unknown routine", original, "nope", ()),
            ("ambiguous inference", original.replace(
                'file_pattern = "*-feed.md"', 'file_pattern = "*-feed.md"\ndigest = { include = false }'
            ), None, ("pass --routine", "feed-digest")),
            ("absent inference", original.replace("digest = { include = false }", ""), None, ("include = false",)),
        )
        for name, document, routine, messages in cases:
            with self.subTest(case=name):
                registry.write_text(document, encoding="utf-8")
                kwargs = {} if routine is None else {"routine_name": routine}
                with self.assertRaises(SystemExit) as caught:
                    rd.write(self.vault, "<h1>x</h1>", self.manifest, **kwargs)
                for message in messages:
                    self.assertIn(message, str(caught.exception))

    def test_dry_run_reports_the_path_without_writing(self):
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
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = rd.write(
                self.vault,
                "x" * (core.GMAIL_CLIP_BYTES + 1),
                self.manifest,
                routine_name="digest-writer",
            )
        self.assertEqual(code, 0)
        self.assertIn("Gmail clips", buffer.getvalue())
        self.assertTrue(
            (self.vault / "inbox" / "digest" / "2099-01-30-weekly-digest.html").is_file()
        )


class AckTests(VaultCase):
    def setUp(self):
        super().setUp()
        self.ack_path = self.vault / "_meta" / "routine_acks.json"

    def test_ack_updates_only_newer_manifest_directories(self):
        cases = (
            ("newest names", {}, {"inbox/feed": "2099-01-30-feed.md", "career/scans": "role_scan_2099-01-28.md"}),
            ("never backwards", {"inbox/feed": "2099-02-99-feed.md"}, {"inbox/feed": "2099-02-99-feed.md"}),
            ("unrelated directory", {"some/other/dir": "keep-me.md"}, {"some/other/dir": "keep-me.md"}),
        )
        for name, initial, expected in cases:
            with self.subTest(case=name):
                self.ack_path.write_text(json.dumps(initial), encoding="utf-8")
                rd.ack(self.vault, self.manifest)
                data = json.loads(self.ack_path.read_text(encoding="utf-8"))
                self.assertEqual({key: data[key] for key in expected}, expected)

    def test_ack_dry_run_does_not_write(self):
        rd.ack(self.vault, self.manifest, dry_run=True)
        self.assertFalse(self.ack_path.exists())

    def test_ack_cue_tracks_only_included_routines(self):
        import cues

        for name, include_maintenance in (("excluded remains outstanding", False), ("included clears", True)):
            with self.subTest(case=name):
                self.ack_path.unlink(missing_ok=True)
                manifest = rc.collect(
                    self.vault, mode="weekly", until="2099-01-30", unacked=True,
                    include_maintenance=include_maintenance,
                )
                rd.ack(self.vault, manifest)
                cue, _debug = cues.check_routine_outputs(self.vault, rc.effective_date())
                if include_maintenance:
                    self.assertIsNone(cue, cue.message if cue else "")
                else:
                    self.assertIsNotNone(cue)
                    self.assertIn("maintenance output", cue.message)
                    self.assertNotIn("daily feed digest", cue.message)


class CliTests(VaultCase):
    def test_brief_only_daily_manifest_still_writes(self):
        manifest_path = Path(self.tmp.name) / "empty.json"
        manifest_path.write_text(json.dumps(rc.collect(self.vault, mode="daily", until="2020-01-01")))
        brief_path = Path(self.tmp.name) / "brief.json"
        brief = self._brief({"text": "Submit the example renewal today", "days_left": 0})
        brief["date"] = "2020-01-01"
        brief_path.write_text(json.dumps(brief))
        proc = self._run("write", "--manifest", str(manifest_path), "--brief", str(brief_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = self.vault / "inbox/digest/2020-01-01-daily-digest.html"
        self.assertTrue(written.is_file(), proc.stdout)
        self.assertIn("Submit the example renewal today", written.read_text())

    def test_non_object_manifests_fail_without_a_traceback(self):
        path = Path(self.tmp.name) / "manifest.json"
        for value in ([], None, 1):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                proc = self._run("write", "--manifest", str(path))
                self.assertEqual(proc.returncode, 1)
                self.assertIn("must be a JSON object", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

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
            "collect", "--mode", "daily", "--until", "2099-01-31", "--days", "1",
            "--json", "--out", str(manifest_path),
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


    def test_collect_emits_a_json_manifest(self):
        proc = self._run("collect", "--until", "2099-01-30", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        manifest = json.loads(proc.stdout)
        for key in ("schema", "window", "counts", "lanes"):
            self.assertEqual(manifest[key], self.manifest[key])

    def test_retired_interfaces_fail_before_io(self):
        for args in (("collect",), ("collect", "--json", "--feeds", "missing.json"),
                     ("render", "--manifest", "missing.json")):
            with self.subTest(args=args):
                proc = self._run(*args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("usage:", proc.stderr)

    def test_missing_registry_fails_loudly(self):
        (self.vault / "_tools/routines/registry.toml").unlink()
        proc = self._run("collect", "--json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("routine registry missing", proc.stderr)


class MailTests(VaultCase):
    """Private configuration controls delivery, never model-provided recipients."""

    def _config(self, body: str) -> None:
        (self.vault / "_meta" / "mail.toml").write_text(body, encoding="utf-8")

    def test_mail_configuration_names_missing_fields_and_accepts_either_secret_source(self):
        for body, missing in (
            (None, ("mail config missing",)),
            ('[smtp]\nhost = "smtp.example.com"\n', ("username",)),
            ('[smtp]\nhost = "h"\nusername = "a@b.c"\n', ("keychain_service", "password_file")),
        ):
            with self.subTest(missing=missing):
                if body is not None:
                    self._config(body)
                with self.assertRaises(SystemExit) as caught:
                    rm.load_mail_config(self.vault)
                for field in missing:
                    self.assertIn(field, str(caught.exception))
        for source, value in (("password_file", "/x"), ("keychain_service", "svc")):
            with self.subTest(source=source):
                self._config('[smtp]\nhost = "smtp.example.com"\nport = 587\n'
                             f'username = "someone@example.com"\n{source} = "{value}"\n')
                smtp = rm.load_mail_config(self.vault)
                self.assertEqual(smtp["username"], "someone@example.com")
                self.assertEqual(smtp[source], value)

    def test_message_preserves_the_artifact_and_only_addresses_the_configured_account(self):
        for body in ("<h1>Atelier Daily</h1><p>unique-marker-9f3</p>", "<p>中文</p>\n"):
            with self.subTest(body=body):
                message = rm.build_message(body, "Subject", "someone@example.com", "someone@example.com")
                self.assertEqual(message["To"], "someone@example.com")
                self.assertIsNone(message["Cc"])
                self.assertIsNone(message["Bcc"])
                html = next(part for part in message.walk() if part.get_content_type() == "text/html")
                # MIME adds a terminal newline when the source lacks one.
                self.assertEqual(html.get_content(), body if body.endswith("\n") else body + "\n")

    def test_rendered_artifact_bytes_are_the_bytes_attached_to_mail(self):
        manifest, overview, brief, retrospect, context = representative_digest_inputs("daily")
        document = rr.render(manifest, overview, brief, retrospect, context)
        self.assertTrue(document.endswith("\n"))
        self.assertEqual(
            rd.write(self.vault, document, manifest, routine_name="digest-writer"),
            0,
        )
        artifact = self.vault / "inbox/digest/2099-01-30-daily-digest.html"
        message = rm.build_message(document, rr.digest_title(manifest), "someone@example.com", "someone@example.com")
        html_part = next(part for part in message.walk() if part.get_content_type() == "text/html")
        self.assertEqual(html_part.get_payload(decode=True), artifact.read_bytes())

    def test_dry_run_sends_nothing_and_names_the_destination(self):
        self._config(
            '[smtp]\nhost = "smtp.example.com"\nport = 587\n'
            'username = "someone@example.com"\nkeychain_service = "svc"\n'
        )
        buffer = io.StringIO()
        with (contextlib.redirect_stdout(buffer), patch("smtplib.SMTP") as smtp,
              patch("routine_mail.smtp_password") as password):
            code = rm.mail(self.vault, "<h1>x</h1>", "Subject", dry_run=True)
        smtp.assert_not_called()
        password.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("someone@example.com", buffer.getvalue())
        self.assertIn("smtp.example.com:587", buffer.getvalue())

    def test_the_recipient_cannot_be_overridden_from_the_command_line(self):
        for flag in ("--to", "--recipient"):
            with self.subTest(flag=flag):
                proc = self._run("mail", "--html", "unused.html", "--subject", "S", flag, "other@example.com")
                self.assertEqual(proc.returncode, 2)
                self.assertIn(f"unrecognized arguments: {flag}", proc.stderr)


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
            rm.smtp_password({"username": "a@b.c", "password_file": str(path)})
        self.assertIn("0600", str(caught.exception))

    def test_a_protected_password_file_is_read(self):
        path = self._file("abcd efgh ijkl mnop")
        got = rm.smtp_password({"username": "a@b.c", "password_file": str(path)})
        self.assertEqual(got, "abcdefghijklmnop", "spaces must be stripped")

    def test_a_missing_file_and_no_keychain_reports_both_attempts(self):
        command = [
            "security", "find-generic-password", "-w", "-s",
            "definitely-not-a-real-service", "-a", "a@b.c",
        ]
        result = subprocess.CompletedProcess(
            command, 44, stdout="", stderr="fixture keychain unavailable"
        )
        with (
            patch("routine_mail.subprocess.run", return_value=result) as run,
            self.assertRaises(SystemExit) as caught,
        ):
            rm.smtp_password(
                {
                    "username": "a@b.c",
                    "keychain_service": "definitely-not-a-real-service",
                    "password_file": str(self.dir / "absent"),
                }
            )
        run.assert_called_once_with(
            command, capture_output=True, text=True, timeout=rm.KEYCHAIN_TIMEOUT_SECONDS
        )
        self.assertLessEqual(rm.KEYCHAIN_TIMEOUT_SECONDS, 30)
        message = str(caught.exception)
        self.assertIn("keychain", message)
        self.assertIn("fixture keychain unavailable", message)
        self.assertIn("missing", message)


class EmailSafeStylingTests(VaultCase):
    """The final artifact must work without stylesheets or external assets."""

    def setUp(self):
        super().setUp()
        self.brief = self._brief(
            {"text": "a forfeitable thing", "source": "finance/x.md:1"},
            kind="closing", heading="需要开始处理 1 件",
        )
        self.brief["groups"].append(
            {"tier": 3, "kind": "review", "heading": "review 债 2 项", "folded": True, "items": []}
        )
        self.brief["warnings"] = ["deadline index missing"]

    def test_mail_artifact_is_self_contained_and_prioritizes_action(self):
        document = rr.render(self.manifest, {}, self.brief)
        parsed = ParsedDocument(document)
        for forbidden in ("@font-face", "fonts.googleapis", "<img", "background-image", "url("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, document)
        self.assertEqual(parsed.root.find_all("script"), [])
        self.assertEqual(parsed.root.find_all("img"), [])
        self.assertFalse(
            any(node.attrs.get("rel") == "stylesheet" for node in parsed.root.find_all("link"))
        )
        for tag in ("h1", "h2", "ul", "li"):
            with self.subTest(tag=tag):
                nodes = parsed.root.find_all(tag)
                self.assertTrue(nodes)
                self.assertTrue(all(node.attrs.get("style") for node in nodes))
        self.assertTrue(
            any("border-top:" in (node.attrs.get("style") or "") for node in parsed.root.find_all("p"))
        )
        self.assertEqual(document.count("a forfeitable thing"), 1)
        self.assertEqual(document.count("今日 <span"), 1)
        self.assertLess(document.index("a forfeitable thing"), document.index("review 债"))
        warning = parsed.root.find_all(class_name="warning")
        self.assertEqual(len(warning), 1)
        self.assertIn("! deadline index missing", warning[0].text)


class FoldTests(VaultCase):
    """The scan layer is bounded; optional depth is priced and review-gated."""

    def setUp(self):
        super().setUp()
        self.overview = {"schema": 1, "headline": "一句话", "sections": [
            {"title": "信号", "bullets": [{"text": "cross-source movement"}]}]}
        self.picks = [
            {"path": "wiki/Old.md", "tier": "wiki", "title": "An Old Idea",
             "age_days": 400, "excerpt": "旧笔记的正文摘录。", "reviewed": True},
            {"path": "reflections/Private.md", "tier": "reflections", "title": "Sensitive",
             "age_days": 200, "excerpt": "must never appear", "reviewed": False},
        ]

    def test_scan_depth_and_reviewed_recall_share_one_priced_artifact(self):
        document = rr.render(self.manifest, self.overview, None, self.picks)
        fold = document.index("以上 ")
        self.assertIn("分钟读完", document)
        self.assertIn("按需", document)
        self.assertLess(document.index("信号"), fold)
        for heading in ("情报详读", "随机回顾", "Source index"):
            with self.subTest(heading=heading):
                self.assertGreater(document.index(heading), fold)
        for heading in ("情报详读", "随机回顾"):
            section = document[document.index(heading):].split("</h2>", 1)[0]
            self.assertRegex(section, r"\d+ 分钟")
        self.assertIn("An Old Idea", document)
        self.assertNotIn("must never appear", document)
        self.assertNotIn("Sensitive", document)
        text = ParsedDocument(document).text
        deep = text[text.index("情报详读"):text.index("随机回顾")]
        index = text[text.index("Source index"):]
        self.assertIn("a tighter path raises discount rates", deep)
        self.assertGreater(len(deep), len(index))


class ReadingCostTests(unittest.TestCase):
    def test_empty_text_costs_nothing(self):
        self.assertEqual(rr.reading_minutes(""), 0)
        self.assertEqual(rr.reading_minutes("<div></div>"), 0)

    def test_any_real_text_costs_at_least_a_minute(self):
        """Rounding to zero would read as "free", which no section is."""
        self.assertEqual(rr.reading_minutes("<p>短</p>"), 1)

    def test_markup_is_not_counted_as_prose(self):
        bare = rr.reading_minutes("字" * 700)
        wrapped = rr.reading_minutes(f'<div style="margin:0;padding:20px;"><p>{"字" * 700}</p></div>')
        self.assertEqual(bare, wrapped)

    def test_cjk_and_latin_are_priced_separately(self):
        self.assertGreater(rr.reading_minutes("字" * 1000), 1)
        self.assertGreaterEqual(rr.reading_minutes(" ".join(["word"] * 700)), 3)


class ArticleSectionTests(VaultCase):
    """Articles offer an abstract before asking the reader to open a link."""

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

    def test_article_selection_is_above_the_fold_with_link_and_abstract(self):
        html = rr.render(self.manifest, {"schema": 1, "articles": [self._one()]})
        self.assertIn('href="https://read.readwise.io/read/abc"', html)
        self.assertIn("A Paper", html)
        self.assertIn("13 mins", html)
        self.assertNotIn("13 mins min", html)
        self.assertIn("1 篇 · 13 分钟", html)
        self.assertIn("服务 multimodal 方向", html)
        self.assertIn("GOP 分块存储", html)
        self.assertLess(html.index("A Paper"), html.index("以上 "))

    def test_article_durations_keep_units_and_do_not_total_unknown_values(self):
        for value, label, minutes in (
            (13, "13 min", 13), ("13", "13 min", 13),
            ("1 hr 5 mins", "1 hr 5 mins", 65), ("2 hrs", "2 hrs", 120),
            (13.5, "13.5 min", 13.5), ("13.5", "13.5 min", 13.5),
            ("13.5 mins", "13.5 mins", 13.5), ("unknown", "unknown", None),
            (None, "", None), ("<b>x</b>", "<b>x</b>", None),
        ):
            with self.subTest(value=value):
                article = self._one(minutes=value, source="")
                parsed = ParsedDocument(rr._node_text(rr._render_articles([article])))
                meta = parsed.root.find_all(class_name="article-meta")
                self.assertEqual(meta[0].text if meta else "", label)
                badge = ParsedDocument(rr._articles_badge([article])).text
                self.assertEqual(badge, f"1 篇 · {minutes:g} 分钟" if minutes else "1 篇")
        self.assertEqual(
            ParsedDocument(rr._articles_badge([self._one(minutes=13), self._one(minutes="unknown")])).text,
            "2 篇",
        )

    def test_incomplete_articles_are_dropped(self):
        for missing in ({"abstract": ""}, {"abstract": "   "}, {"title": ""}):
            with self.subTest(missing=missing):
                document = rr.render(
                    self.manifest, {"schema": 1, "articles": [self._one(**missing)]}
                )
                self.assertNotIn("A Paper", ParsedDocument(document).text)

    def test_optional_fields_degrade_rather_than_break(self):
        document = rr.render(
            self.manifest,
            {"schema": 1, "articles": [self._one(url="", minutes="", source="", why="")]},
        )
        parsed = ParsedDocument(document)
        articles = parsed.root.find_all(class_name="article")
        self.assertEqual(len(articles), 1)
        self.assertIn("A Paper", articles[0].text)
        self.assertEqual(articles[0].find_all("a"), [])

    def test_article_text_cannot_inject_markup(self):
        html = rr.render(
            self.manifest,
            {"schema": 1, "articles": [self._one(title="<script>x</script>", abstract="<b>not bold</b>")]},
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>not bold</b>", html)

class MarkdownSubsetTests(unittest.TestCase):
    """Routine reports are Markdown and are data, never instruction."""

    def test_content_is_escaped_before_any_pattern_runs(self):
        html, _ = rr.markdown_to_html("# <script>alert(1)</script>\n\ntext")
        self.assertNotIn("<script>", html)

    def test_headings_lists_and_tables_survive(self):
        html, _ = rr.markdown_to_html(
            "## Findings\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        )
        self.assertIn("Findings", html)
        self.assertIn("<li", html)
        self.assertIn("<table", html)
        self.assertIn("<th", html)

    def test_images_are_removed_not_rendered(self):
        """Mail clients block remote images; they cost bytes and show a box."""
        html, _ = rr.markdown_to_html("![alt](https://example.com/x.png)\n\nreal text")
        self.assertNotIn("<img", html)
        self.assertNotIn("example.com/x.png", html)
        self.assertIn("real text", html)

    def test_commonmark_structure_and_mail_styles(self):
        markdown = (
            "## Findings\n\n3. outer\n   - **内层**\n\n"
            "| a | b |\n|---|---|\n| x\\|y | `**literal**` |\n\n"
            "[source](https://example.com/a_(b)?x=1&y=2)"
        )
        html, _ = rr.markdown_to_html(markdown)
        for expected in ('start="3"', "<strong>内层</strong>", "x|y", "**literal**",
                         'href="https://example.com/a_(b)?x=1&amp;y=2"'):
            self.assertIn(expected, html)
        self.assertNotIn("<strong>literal</strong>", html)

        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            source = vault / "inbox/feed/2099-01-30-feed.md"
            source.write_text(markdown, encoding="utf-8")
            previous = _set_vault(vault)
            try:
                manifest = rc.collect(vault, mode="daily", until="2099-01-30")
                document = rr.render(manifest, {"schema": 1})
            finally:
                _restore_vault(previous)
        parsed = ParsedDocument(document)
        headings = [node for node in parsed.root.find_all("h2") if "Findings" in node.text]
        self.assertEqual(len(headings), 1)
        self.assertIn("font-weight:", headings[0].attrs.get("style") or "")
        for tag in ("li", "td", "code", "a"):
            with self.subTest(tag=tag):
                self.assertTrue(
                    any(node.attrs.get("style") for node in parsed.root.find_all(tag)),
                    f"expected compiled inline style on <{tag}>",
                )

    def test_untrusted_markup_has_no_active_content_or_non_http_links(self):
        for text in ('<script>alert(1)</script><img src="https://example.com/x">',
                     '[click](javascript:alert(1))', '[click](jav&#x61;script:alert(1))',
                     '[click](data:text/html,payload)', '[click](file:///etc/passwd)',
                     '[click](//example.com/path)', '[click](mailto:someone@example.com)',
                     '[click](https://example.com/\"onmouseover=\"alert(1))'):
            with self.subTest(text=text):
                for rendered in (rr.inline_html(text), rr.markdown_to_html(text)[0]):
                    self.assertNotRegex(rendered, r'<(?:script|img)\b|\shref="(?:javascript:|data:|file:|//|mailto:)|\sonmouseover="')

    def test_reference_images_and_fences_remain_omitted_without_io(self):
        with patch("socket.socket", side_effect=AssertionError("rendering must be offline")), \
             patch("builtins.open", side_effect=AssertionError("Markdown is not a file include")):
            html, _ = rr.markdown_to_html(
                "![remote][pic]\n\n[pic]: https://example.com/x.png\n\n"
                "```html\n<script>fenced payload</script>\n```\n\nvisible"
            )
        for omitted in ("<img", "example.com/x.png", "fenced payload"):
            self.assertNotIn(omitted, html)
        self.assertIn("visible", html)

    def test_render_dependency_is_not_required_to_import_mail_or_check(self):
        result = subprocess.run(
            [sys.executable, "-S", "-c", "import sys; sys.path.insert(0, 'scripts'); "
             "import routine_digest; assert routine_digest.check_html('<p>safe</p>') == []"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_digest_interpreter_resolves_dependencies_and_fails_closed(self):
        for modules, expected in (([], 0), (["markdown_it"], 0), (["atelier_nonexistent_module"], 1)):
            with self.subTest(modules=modules):
                result = subprocess.run(
                    ["bash", "scripts/find_python.sh", *modules], cwd=REPO_ROOT,
                    env={**os.environ, "ATELIER_PYTHON": sys.executable}, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 0:
                    self.assertTrue(Path(result.stdout.strip()).is_absolute())

    def test_a_long_report_is_truncated_and_says_so(self):
        html, truncated = rr.markdown_to_html("word " * 5000, limit=500)
        self.assertTrue(truncated)
        self.assertLess(len(html), 3000)

    def test_a_short_report_is_not_flagged_truncated(self):
        _html, truncated = rr.markdown_to_html("just a line", limit=500)
        self.assertFalse(truncated)

    def test_nesting_limit_never_silently_discards_report_content(self):
        for prefix in ("> " * 19, "> " * 20, "> " * 21, "- " * 12):
            with self.subTest(prefix=prefix):
                html, truncated = rr.markdown_to_html(prefix + "visible <script>payload</script>")
                self.assertIn("visible", html)
                self.assertNotIn("<script>", html)
                self.assertFalse(truncated)
        overflow, _ = rr.markdown_to_html("> " * 21 + "visible\n\n```\nfenced text\n```\n\n"
                                          "![image][pic]\n\n[pic]: https://example.com/image.png")
        self.assertIn("纯文本", overflow)
        self.assertIn("fenced text", overflow)  # Explicit overflow shows inert source, not a second parser.
        self.assertNotIn("<img", overflow)

    def test_leading_frontmatter_is_dropped_but_a_rule_is_kept(self):
        """The raw-body fallback showed `date:` and `type:` as paragraphs."""
        html, _ = rr.markdown_to_html(TECH_DIGEST)
        self.assertNotIn("type: feed", html)
        self.assertNotIn("item_count: 2", html)
        self.assertIn("Daily Feed Digest", html)
        ruled, _ = rr.markdown_to_html("para one\n\n---\n\npara two\n")
        self.assertIn("para one", ruled)
        self.assertIn("para two", ruled)


class AttentionBudgetTests(VaultCase):
    """The renderer enforces each routine's budget, regardless of its writer."""

    def setUp(self):
        super().setUp()
        self.path = next(core.iter_sources(self.manifest))[1]["path"]

    def _render(self, lines: int, cap: int | None = None) -> tuple[str, ParsedDocument]:
        if cap is not None:
            for _lane, source in core.iter_sources(self.manifest):
                source["max_lines"] = cap
        summary = "\n".join(f"第 {n} 行" for n in range(1, lines + 1))
        document = rr.render(
            self.manifest,
            {"schema": 1, "routines": [{"path": self.path, "summary": summary}]},
        )
        return document, ParsedDocument(document)

    def test_a_summary_inside_its_budget_is_untouched(self):
        _document, parsed = self._render(3, cap=5)
        self.assertIn("第 3 行", parsed.text)
        self.assertNotIn("已截至", parsed.text)

    def test_an_overlong_summary_is_cut_and_says_so(self):
        _document, parsed = self._render(12, cap=5)
        self.assertIn("第 5 行", parsed.text)
        self.assertNotIn("第 6 行", parsed.text)
        self.assertIn("已截至 5 行", parsed.text)

    def test_the_registry_supplies_the_cap(self):
        self.assertEqual(
            {s["label"]: s["max_lines"] for _l, s in core.iter_sources(self.manifest)}[
                "daily feed digest"
            ],
            core.DEFAULT_ROUTINE_LINES,
        )

    def test_an_empty_summary_yields_no_entry(self):
        document = rr.render(
            self.manifest,
            {"schema": 1, "routines": [{"path": self.path, "summary": "  "}]},
        )
        self.assertNotIn("routine 摘要", ParsedDocument(document).text)

    def test_summary_text_cannot_inject_markup(self):
        html = rr.render(
            self.manifest,
            {"schema": 1, "routines": [{"path": self.path, "summary": "<script>x</script>"}]},
        )
        self.assertNotIn("<script>", html)

    def test_briefs_sit_above_the_fold_and_bodies_below(self):
        overview = {"schema": 1, "routines": [{"path": self.path, "summary": "一行摘要"}]}
        document = rr.render(self.manifest, overview, None, [])
        self.assertLess(document.index("routine 摘要"), document.index("以上 "))
        self.assertGreater(document.index("情报详读"), document.index("以上 "))


class SharedDirectoryTests(VaultCase):
    """Acks are a per-directory mark; several routines can share a directory.

    Selecting per routine let a batch of one routine's oldest files advance the
    mark past another routine's older, never-shown files. The unit for unacked
    selection is therefore the directory, and `ack` names what it would hide.
    """

    def setUp(self):
        super().setUp()
        registry = self.vault / "_tools/routines/registry.toml"
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

    def test_unacked_batch_walks_a_shared_directory_oldest_first(self):
        manifest = rc.collect(self.vault, mode="weekly", unacked=True, max_files=2)
        names = sorted(
            Path(s["path"]).name for lane in manifest["lanes"] for s in lane["sources"]
        )
        self.assertEqual(names, ["2099-01-20-extra.md", "2099-01-25-monitor.md"])
        self.assertTrue(manifest["truncated"])
        # The mark after acking this batch is exactly its last file: nothing
        # older than it in this directory was skipped.
        self.assertEqual(manifest["acks"], {"finance/signals": "2099-01-25-monitor.md"})

    def test_ack_names_the_files_it_would_hide(self):
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
        self.manifest, _overview, _brief, _retrospect, self.context = representative_digest_inputs()
        self.context["warnings"] = []

    def test_weather_sits_in_the_masthead_and_provenance_in_the_colophon(self):
        document = rr.render(self.manifest, None, None, None, self.context)
        self.assertIn("Lisbon", document)
        self.assertIn("13–25°C", document)
        self.assertIn("9:00 18°", document)
        self.assertLess(document.index("Lisbon"), document.index("Source index"))
        self.assertLess(document.index("Source index"), document.index("KB 源文本"))
        self.assertLess(document.index("Source index"), document.index("生成于 06:22"))

    def test_quota_bar_shows_the_remaining_share_in_its_level_colour(self):
        parsed = ParsedDocument(rr.render(self.manifest, None, None, None, self.context))
        self.assertIn("Harness 额度", parsed.text)
        bars = {
            node.attrs.get("width"): node.attrs.get("style") or ""
            for node in parsed.root.find_all("td")
            if node.attrs.get("width") in {"83%", "15%"}
        }
        self.assertIn(rr._OK, bars["83%"])
        self.assertIn(rr._URGENT, bars["15%"])
        quota_labels = {
            node.text.strip(): node.attrs.get("style") or ""
            for node in parsed.root.find_all("span")
            if node.text.strip() in {"剩 83%", "剩 15%"}
        }
        self.assertIn(rr._OK, quota_labels["剩 83%"])
        self.assertIn(rr._URGENT, quota_labels["剩 15%"])
        self.assertIn("1 天 2 小时后重置", parsed.text)
        self.assertIn("快照 3.0h 前", parsed.text)

    def test_context_is_optional_and_warnings_are_visible(self):
        plain = rr.render(self.manifest)
        self.assertNotIn("Harness 额度", plain)
        warned = rr.render(self.manifest, None, None, None, {"quota": [], "warnings": ["claude quota: no snapshot"]})
        self.assertIn("claude quota: no snapshot", warned)

    def test_signal_strip_shows_only_live_numbers(self):
        """Guard for the 2026-09-01 masthead, which spent its strip on fleet
        bookkeeping (4/19 有产出, 149 待 review) that reads the same every day."""
        brief = {
            "schema": 1,
            "date": "2099-01-30",
            "signals": {"closing": 2, "closing_now": 1, "focus_days": 29, "weight_age_days": 143},
            "groups": [],
            "warnings": [],
        }
        overview = {
            "schema": 1,
            "sections": [
                {
                    "title": "需要的决策",
                    "bullets": [{"text": "a", "between": ["b", "c"], "settles": "d"}],
                }
            ],
        }
        parsed = ParsedDocument(rr.render(self.manifest, overview, brief))
        numbers = parsed.root.find_all("span", class_name="signal-number")
        labels = parsed.root.find_all("span", class_name="signal-label")
        self.assertEqual(len(numbers), 4)
        self.assertEqual({label.text.strip() for label in labels}, {"关窗", "主线", "体重", "决策"})
        styles = {node.text.strip(): node.attrs.get("style") or "" for node in numbers}
        self.assertIn(rr._URGENT, styles["2"])  # one closes today
        self.assertIn(rr._ACCENT, styles["29d"])
        self.assertIn(rr._URGENT, styles["143d"])  # past WEIGHT_STALE_DAYS
        for noise in ("有产出", "待 review", "完成", "失败"):
            self.assertNotIn(noise, " ".join(label.text for label in labels))

    def test_signal_strip_is_empty_when_nothing_is_live(self):
        plain = ParsedDocument(rr.render(self.manifest))
        zero = ParsedDocument(
            rr.render(self.manifest, {"schema": 1, "sections": []}, {"signals": {"closing": 0}})
        )
        self.assertEqual(plain.root.find_all(class_name="signal-number"), [])
        self.assertEqual(zero.root.find_all(class_name="signal-number"), [])

        failed_manifest = representative_digest_inputs()[0]
        failed_manifest["health"]["failed"] = 3
        failed = ParsedDocument(rr.render(failed_manifest))
        failed_number = failed.root.find_all("span", class_name="signal-number")
        self.assertEqual(len(failed_number), 1)
        self.assertEqual(failed_number[0].text.strip(), "3")
        self.assertIn(rr._URGENT, failed_number[0].attrs.get("style") or "")
        self.assertEqual(failed.root.find_all("span", class_name="signal-label")[0].text.strip(), "失败")

        for age, color in ((3, rr._INK), (4, rr._URGENT)):
            with self.subTest(weight_age_days=age):
                brief = {"signals": {"weight_age_days": age}}
                parsed = ParsedDocument(rr.render(self.manifest, None, brief))
                number = parsed.root.find_all("span", class_name="signal-number")
                self.assertEqual(len(number), 1)
                self.assertEqual(number[0].text.strip(), f"{age}d")
                self.assertIn(color, number[0].attrs.get("style") or "")

    def test_fleet_bookkeeping_moves_to_the_colophon(self):
        brief = {
            "schema": 1,
            "date": "2099-01-30",
            "signals": {},
            "groups": [
                {"tier": 3, "kind": "recurring", "heading": "recurring: 9 条逾期, 4 条刚到期", "folded": True, "items": []},
            ],
            "warnings": [],
        }
        document = rr.render(self.manifest, {"schema": 1}, brief)
        self.assertNotIn(rr._S_STAT_TABLE, document)
        for bit in ("routine 4/19 有产出", "2 完成", "149 待 review", "recurring 逾期 9"):
            self.assertIn(bit, document)
            self.assertGreater(document.index(bit), document.index("Source index"))

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
        parsed = ParsedDocument(rr.render(self.manifest, None, brief))
        due = parsed.root.find_all("td", class_name="ledger-due")
        self.assertEqual([node.text.strip() for node in due], ["42d", "今天", "4d", "逾期 2d"])
        styles = {
            node.text.strip(): (node.find_all("span")[0].attrs.get("style") or "")
            for node in due
        }
        self.assertIn(rr._ACCENT, styles["42d"])
        self.assertIn(rr._URGENT, styles["今天"])
        self.assertIn("font-weight:600", styles["今天"])
        self.assertIn(rr._MUTED, styles["4d"])
        self.assertIn("今日", parsed.text)
        self.assertIn("· 4 件", parsed.text)
        self.assertIn("x:1", parsed.text)


class FrontierAndCuratedDepthTests(unittest.TestCase):
    def setUp(self):
        self.manifest = representative_digest_inputs()[0]

    def _labs(self, sweep_date):
        labs = representative_digest_inputs()[1]["frontier_labs"]
        labs["sweep_date"] = sweep_date
        return labs

    def test_frontier_renders_the_table_on_the_sweep_day_and_the_day_after(self):
        for sweep in ("2099-01-30", "2099-01-29"):
            document = rr.render(self.manifest, {"schema": 1, "frontier_labs": self._labs(sweep)})
            self.assertIn("前沿实验室", document)
            self.assertIn("1 条信号 · 0 漂移 · 1 晋级 · 扫描 " + sweep[5:], document)
            self.assertIn("Example Lab", document)
            self.assertIn('href="https://example.com/atlas"', document)
            self.assertIn("No mission drift in the synthetic fixture.", document)
            self.assertLess(document.index("前沿实验室"), document.index("以上 "))

    def test_frontier_collapses_to_counts_on_later_days(self):
        document = rr.render(self.manifest, {"schema": 1, "frontier_labs": self._labs("2099-01-25")})
        self.assertIn("1 条信号 · 0 漂移 · 1 晋级 · 扫描 01-25", document)
        self.assertNotIn("Example Lab", document)
        self.assertIn("已随当日 digest 报告", document)

    def test_frontier_is_absent_without_the_field(self):
        self.assertNotIn("前沿实验室", rr.render(self.manifest, {"schema": 1}))

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
        document = rr.render(self.manifest, overview)
        self.assertIn("信号精选 · 1 / 5", document)
        self.assertIn("第一点。", document)
        self.assertIn("第二点。", document)
        self.assertNotIn("第三点不该出现。", document)
        self.assertRegex(document, r"<(b|strong)[^>]*>MU</")
        self.assertIn('href="https://example.com/hbm"', document)
        # The raw body is gone from the depth layer; the source index below it
        # still carries its excerpt, which is navigation, not depth.
        depth = document[document.index("以上 "):document.index("Source index")]
        self.assertNotIn("The rate corridor was held", depth)
        # The feed's own items follow, deterministically, from the manifest.
        self.assertIn("科技动态 · 1", document)
        self.assertIn('href="https://example.com/model"', document)
        self.assertIn("示例模型发布了可复核的更新。", document)
        self.assertIn("信号 1 / 5 · 科技动态 1", document)
        self.assertGreater(document.index("信号精选"), document.index("以上 "))

    def _finance_only_pick(self, lane=None) -> dict:
        entry = {"title": "HBM 良率", "url": "https://example.com/hbm", "facts": ["一。"], "why": "为什么。"}
        if lane:
            entry["lane"] = lane
        return {"schema": 1, "deep_read": {"total": 3, "entries": [entry]}}

    def test_depth_that_skips_research_on_a_research_window_is_flagged(self):
        """Guard for the finance-dominated pick: 2026-09-01 shipped three finance
        entries and zero research on a window that had a research source."""
        gap = core.deep_read_lane_gap(self._finance_only_pick()["deep_read"], self.manifest)
        self.assertIsNotNone(gap)
        self.assertIn("1 个 Research 来源", gap)
        document = rr.render(self.manifest, self._finance_only_pick())
        self.assertIn("! 情报精选没有 Research 条目", document)
        self.assertGreater(document.index("! 情报精选"), document.index("情报详读"))

    def test_research_entry_or_research_free_window_is_silent(self):
        self.assertIsNone(core.deep_read_lane_gap(self._finance_only_pick("Research")["deep_read"], self.manifest))
        research_free = representative_digest_inputs()[0]
        research_free["lanes"] = [
            lane for lane in research_free["lanes"] if lane.get("lane") != "Research"
        ]
        self.assertIsNone(core.deep_read_lane_gap(self._finance_only_pick()["deep_read"], research_free))
        self.assertIsNone(core.deep_read_lane_gap({"total": 0, "entries": []}, self.manifest))
        self.assertNotIn("! 情报精选", rr.render(self.manifest, self._finance_only_pick("Research")))

    def test_without_curation_the_raw_fallback_still_renders(self):
        document = rr.render(self.manifest, {"schema": 1})
        self.assertNotIn("信号精选", document)
        self.assertIn("情报详读", document)
        self.assertIn("A compact systems result with a bounded source trail.", document)



class ReviewFollowUpTests(unittest.TestCase):
    """Cross-file contracts and the fail-closed paths the system review asked for."""

    def test_schema_constants_agree_across_modules(self):
        import daily_brief
        import daily_context

        self.assertEqual(rc.BRIEF_SCHEMA, daily_brief.BRIEF_SCHEMA)
        self.assertEqual(rc.CONTEXT_SCHEMA, daily_context.CONTEXT_SCHEMA)

    def test_malformed_overview_section_is_skipped_not_fatal(self):
        manifest = {"schema": core.MANIFEST_SCHEMA, "mode": "daily", "window": {"since": "2099-01-30", "until": "2099-01-30"}, "counts": {"files": 0, "bytes": 0}, "lanes": []}
        document = rr.render(manifest, {"schema": 1, "sections": ["not an object", {"title": "信号", "bullets": [{"text": "ok"}]}]})
        self.assertIn("malformed overview section skipped", document)
        self.assertIn("信号", document)

    def test_frontier_fails_closed_without_a_digest_date(self):
        labs = {"sweep_date": "2099-01-30", "signals": [{"lab": "Example Lab", "text": "Atlas."}]}
        manifest = representative_digest_inputs()[0]
        undated = representative_digest_inputs()[0]
        undated["window"]["until"] = ""
        overview = {"schema": 1, "frontier_labs": labs}
        self.assertNotIn("Example Lab", ParsedDocument(rr.render(undated, overview)).text)
        self.assertIn("Example Lab", ParsedDocument(rr.render(manifest, overview)).text)

    def test_brief_counts_round_trip_through_daily_brief_wording(self):
        """The strip parses daily_brief's own heading, so build that heading with
        daily_brief itself rather than a hand-typed imitation."""
        import daily_brief
        import recurring
        from datetime import date as _date, timedelta as _td

        today = _date(2099, 1, 30)
        rows = [
            recurring.Recurring(
                slug=slug,
                every_n=30,
                every_unit="d",
                last_done=(today + _td(days=days - 30)).isoformat(),
                area=None,
                section=None,
                line=line,
            )
            for slug, days, line in (("a", -40, 1), ("b", -3, 2), ("c", 5, 3))
        ]
        with patch.object(recurring, "parse_file", return_value=rows):
            groups = daily_brief.load_recurring(Path("."), today, [])
        self.assertEqual(len(groups), 1)
        brief = {"groups": [{"kind": groups[0].kind, "heading": groups[0].display_heading(), "items": []}]}
        self.assertEqual(rr._brief_counts(brief)["recurring_overdue"], 2)

    def test_password_file_under_the_vault_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            secret = vault / "_meta" / "smtp.txt"
            secret.write_text("app password\n", encoding="utf-8")
            secret.chmod(0o600)
            previous = _set_vault(vault)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    rm.smtp_password({"username": "someone@example.com", "password_file": str(secret)})
            finally:
                _restore_vault(previous)
            self.assertIn("under $OV", str(ctx.exception))


class WriteTimeGuardTests(VaultCase):
    """Guards for shapes an earlier digest shipped with: a countdown
    printed twice per ledger row, a tech feed of bare headlines, a decision
    that was prose under a heading, a lab table that was mostly blank."""

    def setUp(self):
        super().setUp()
        self.manifest = representative_digest_inputs()[0]
        self.manifest["lanes"] = [lane for lane in self.manifest["lanes"] if lane["lane"] == "Tech feed"]
        self.manifest["lanes"][0]["sources"][0]["items"] = [
            {"title": "One", "url": "https://example.com/one", "note": "第一条的中文摘要。"},
            {"title": "Two", "url": "https://example.com/two", "note": "第二条的中文摘要。"},
        ]

    def _brief_document(self, item: dict) -> str:
        return rr.render(self.manifest, None, self._brief(item))

    def test_ledger_row_prints_the_countdown_once_and_the_hint_below(self):
        document = self._brief_document(
            {
                "text": "Hotel credit · 42d · book one night",
                "label": "Hotel credit",
                "hint": "book one night",
                "days_left": 42,
                "source": "finance/2099-01-30-decision-example-long-note-name.md:123",
            }
        )
        parsed = ParsedDocument(document)
        ledger = parsed.root.find_all("tr", class_name="ledger-row")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0].text.count("42d"), 1)
        self.assertIn("Hotel credit", ledger[0].text)
        hints = ledger[0].find_all("span", class_name="hint")
        self.assertEqual([node.text.strip() for node in hints], ["book one night"])
        self.assertNotIn("Hotel credit · 42d", ledger[0].text)
        # The trace chip keeps enough of the stem to recognise the file.
        self.assertIn("2099-01-30-decision-e…:123", ledger[0].text)
        self.assertNotIn("long-note-name.md", ledger[0].text)
        self.assertEqual(rr.check_html(document), [])

    def test_check_catches_the_old_row_shape_with_the_countdown_in_the_text(self):
        """Mutation: a brief written before `label` existed renders the composed
        line, and the check names the duplicate rather than letting it ship."""
        document = self._brief_document(
            {"text": "Hotel credit", "label": "Hotel credit", "days_left": 42}
        )
        mutated = document.replace(">Hotel credit<", ">Hotel credit · 42d<", 1)
        self.assertNotEqual(mutated, document)
        findings = rr.check_html(mutated)
        self.assertEqual(len(findings), 1)
        self.assertIn("42d", findings[0])
        self.assertIn("重复", findings[0])

    def test_a_label_that_starts_with_today_is_not_a_repeated_countdown(self):
        document = self._brief_document(
            {"text": "今天 · 今天提交护照", "label": "今天提交护照", "days_left": 0}
        )
        self.assertEqual(rr.check_html(document), [])
        old_shape = document.replace(">今天提交护照<", ">今天 · 提交护照<", 1)
        self.assertNotEqual(old_shape, document)
        self.assertEqual(len(rr.check_html(old_shape)), 1)
        self.assertTrue(rr._countdown_repeated("明天", "Hotel · 明天 · book"))
        self.assertFalse(rr._countdown_repeated("明天", "明天的会议"))
        self.assertTrue(rr._countdown_repeated("26d", "恢复健康基线 · 26d"))
        self.assertFalse(rr._countdown_repeated("26d", "Run 126d plan"))
        self.assertTrue(rr._countdown_repeated("逾期 3d", "逾期 3d · 提交表格"))

    def test_reconciliation_flag_renders_on_the_row(self):
        document = self._brief_document(
            {
                "text": "Example Hotel 免房券 · 39d",
                "label": "Example Hotel 免房券",
                "days_left": 39,
                "source": "finance/example-tracker.md:51",
                "flag": "待核",
                "flag_source": "travel/trips/Example City 2099-01-05 to 01-08.md:13",
            }
        )
        parsed = ParsedDocument(document)
        flags = parsed.root.find_all("span", class_name="flag")
        self.assertEqual([node.text.strip() for node in flags], ["待核 · Example City 2099-01-…:13"])

    def _decision(self, **extra):
        return {
            "schema": 1,
            "sections": [
                {
                    "title": rr.DECISION_SECTION,
                    "bullets": [
                        {
                            "text": "是否把验证预算给 Example Model 2",
                            "sources": ["inbox/feed/2099-01-30-feed.md"],
                            **extra,
                        }
                    ],
                }
            ],
        }

    def test_decision_bullet_renders_as_a_card_with_options_and_settlement(self):
        overview = self._decision(
            between=["现在更新跟踪结论", "继续保留为候选"], settles="完整模型卡与独立基准", by="2099-02-01"
        )
        document = rr.render(self.manifest, overview)
        self.assertIn('<li class="decision"', document)
        for key, option in (("A", "现在更新跟踪结论"), ("B", "继续保留为候选")):
            self.assertRegex(document, fr'<span class="decision-option"[^>]*>{key}</span><span class="decision-option-text">{option}</span>')
        self.assertIn('定案 · <span class="decision-settles">完整模型卡与独立基准 · 截止 2099-02-01</span>', document)
        self.assertIn("2099-01-30-feed.md", document)
        self.assertEqual(rr._decision_count(rr.normalize_decisions(overview)[0]), 1)
        self.assertEqual([f for f in rr.check_html(document) if rr.DECISION_SECTION in f], [])

    def test_unstructured_decision_is_demoted_to_signals_and_named_in_the_colophon(self):
        overview = self._decision()
        normalized, notes = rr.normalize_decisions(overview)
        self.assertEqual(notes, [f"{rr.DECISION_SECTION} #1 缺 between/settles, 已降级为{rr.SIGNAL_SECTION}"])
        self.assertEqual(rr._decision_count(normalized), 0)
        titles = [s["title"] for s in normalized["sections"]]
        self.assertEqual(titles, [rr.DECISION_SECTION, rr.SIGNAL_SECTION])
        self.assertEqual(normalized["sections"][1]["bullets"][0]["text"], "是否把验证预算给 Example Model 2")
        # The caller's overview is untouched; the document carries the note.
        self.assertEqual(len(overview["sections"]), 1)
        document = rr.render(self.manifest, overview)
        self.assertNotIn('class="decision"', document)
        self.assertIn("已降级为信号", document)
        self.assertLess(document.index("是否把验证预算"), document.index("以上 "))

    def test_demotion_into_an_existing_signal_section_leaves_the_caller_untouched(self):
        overview = self._decision()
        overview["sections"].append({"title": rr.SIGNAL_SECTION, "bullets": [{"text": "已有信号"}]})
        first, _ = rr.normalize_decisions(overview)
        second, _ = rr.normalize_decisions(overview)
        self.assertEqual([b["text"] for b in overview["sections"][1]["bullets"]], ["已有信号"])
        for result in (first, second):
            self.assertEqual(
                [b["text"] for b in result["sections"][1]["bullets"]],
                ["已有信号", "是否把验证预算给 Example Model 2"],
            )
        rr.render(self.manifest, overview)
        self.assertEqual(rr.render(self.manifest, overview).count("是否把验证预算给"), 1)

    def test_two_decision_sections_keep_their_own_cards(self):
        valid = {"text": "决策甲", "between": ["a", "b"], "settles": "s"}
        second = {"text": "决策乙", "between": ["c", "d"], "settles": "t"}
        overview = {
            "schema": 1,
            "sections": [
                {"title": rr.DECISION_SECTION, "bullets": [valid, {"text": "伪一"}]},
                {"title": rr.DECISION_SECTION, "bullets": [second, {"text": "伪二"}]},
            ],
        }
        normalized, notes = rr.normalize_decisions(overview)
        decisions = [s for s in normalized["sections"] if s["title"] == rr.DECISION_SECTION]
        self.assertEqual([[b["text"] for b in s["bullets"]] for s in decisions], [["决策甲"], ["决策乙"]])
        signals = [s for s in normalized["sections"] if s["title"] == rr.SIGNAL_SECTION]
        self.assertEqual([b["text"] for b in signals[0]["bullets"]], ["伪一", "伪二"])
        self.assertEqual(len(notes), 2)
        document = rr.render(self.manifest, overview)
        self.assertEqual(document.count("决策甲"), 1)
        self.assertEqual(document.count("决策乙"), 1)
        # The masthead counts what the page shows: two cards, two sections.
        self.assertEqual(rr._decision_count(normalized), 2)

    def test_a_decision_with_one_option_is_not_a_decision(self):
        self.assertEqual(rr.decision_shape_missing({"text": "q", "between": ["only"], "settles": "x"}), ["between"])
        self.assertEqual(rr.decision_shape_missing({"text": "q", "between": ["a", "b"]}), ["settles"])
        self.assertEqual(rr.decision_shape_missing("prose"), ["text", "between", "settles"])

    def test_check_reads_the_card_content_not_just_its_labels(self):
        """A card the renderer was handed with blank options or an empty
        settling condition still carries the 定案 label; the check must look
        at what follows it."""
        good = rr.render(
            self.manifest,
            self._decision(between=["a", "b"], settles="证据", by="2099-02-01"),
        )
        blank = good.replace(">a</span>", "></span>", 1).replace(">b</span>", "></span>", 1)
        blank = blank.replace(">证据 · 截止 2099-02-01</span>", "> · 截止 2099-02-01</span>", 1)
        self.assertNotEqual(blank, good)
        findings = rr.check_html(blank)
        self.assertEqual(findings, [f"{rr.DECISION_SECTION} #1 选项不足两个", f"{rr.DECISION_SECTION} #1 缺定案条件"])
        self.assertEqual(rr.check_html(good), [])
        # Options that open with inline markup still count as options.
        marked = rr.render(
            self.manifest,
            self._decision(
                between=["**现在更新**", "`候选` 保留", "[看](https://example.com/x)"],
                settles="**证据**",
            ),
        )
        self.assertEqual(rr.check_html(marked), [])
        self.assertIn("<strong>现在更新</strong>", marked)

    def test_a_decision_without_a_question_is_demoted_not_rendered_blank(self):
        for bullet in ({"between": ["a", "b"], "settles": "s"}, {"text": "  ", "between": ["a", "b"], "settles": "s"}):
            with self.subTest(bullet=bullet):
                self.assertEqual(rr.decision_shape_missing(bullet), ["text"])
                overview = {"schema": 1, "sections": [{"title": rr.DECISION_SECTION, "bullets": [bullet]}]}
                normalized, notes = rr.normalize_decisions(overview)
                self.assertEqual(rr._decision_count(normalized), 0)
                self.assertEqual(notes, [f"{rr.DECISION_SECTION} #1 缺 text, 已降级为{rr.SIGNAL_SECTION}"])
                self.assertNotIn('class="decision"', rr.render(self.manifest, overview))

    def test_frontier_groups_signals_under_one_lab_heading(self):
        labs = {
            "sweep_date": "2099-01-30",
            "drift_count": 0,
            "promotion_count": 0,
            "signals": [
                {"lab": "Example Lab / Long Name (org)", "category": "模型发布", "tier": 1, "text": "Ling 3.0 出现。", "url": "https://example.com/a"},
                {"lab": "Example Lab / Long Name (org)", "category": "图像模型", "tier": 1, "text": "LLaDA-Image 上线。", "url": "https://example.com/b"},
            ],
            "watchlist_note": "无漂移。",
        }
        parsed = ParsedDocument(
            rr.render(self.manifest, {"schema": 1, "frontier_labs": labs})
        )
        names = parsed.root.find_all("p", class_name="lab-name")
        self.assertEqual(len(names), 1)
        self.assertEqual(names[0].text.strip(), "Example Lab / Long Name (org)")
        self.assertNotIn("width:112px", str(names[0].attrs))
        self.assertIn("模型发布 · 1级来源", parsed.text)
        self.assertIn("图像模型 · 1级来源", parsed.text)
        self.assertIn("扫描 01-30", parsed.text)
        self.assertNotIn("周扫", parsed.text)

    def test_inline_dash_summary_becomes_the_item_note(self):
        body = (
            "1. [Example Model](https://example.com/model) — 发布了 Example Model。 "
            "**Cluster:** AI Models · **Mode:** familiar · **Why now:** 相关 · **Provenance:** websearch\n"
        )
        items = rc.extract_items(body, 5)
        self.assertEqual(items[0]["note"], "发布了 Example Model。")

    def test_a_sign_attached_to_a_number_is_not_a_separator(self):
        shapes = {
            "1. [AMD](https://example.com/amd) — -5% after guidance. **Cluster:** x\n": "-5% after guidance.",
            "1. [AMD](https://example.com/amd) - +3% premarket.\n": "+3% premarket.",
            "1. [AMD](https://example.com/amd) -5% after guidance.\n": "-5% after guidance.",
            "1. [AMD](https://example.com/amd)\n   - -2% is the move.\n": "-2% is the move.",
            "1. [AMD](https://example.com/amd) — summary, trailing dash -\n": "summary, trailing dash",
        }
        for body, expected in shapes.items():
            with self.subTest(body=body):
                self.assertEqual(rc.extract_items(body, 5)[0]["note"], expected)

    def test_every_observed_meta_tail_is_cut(self):
        shapes = {
            "1. [T](https://example.com/1)\n   Nvidia says HF stays open. `AI Infra` · **familiar** · Why now: relevant.\n": "Nvidia says HF stays open.",
            "1. [T](https://example.com/2)\n   - 中文摘要。\n   - Cluster: AI | Mode: familiar | Provenance: rss\n": "中文摘要。",
            "1. [T](https://example.com/3)\n   AMD's release adds a layer. **Tag:** GPU. **Mode:** familiar.\n": "AMD's release adds a layer.",
            "1. [T](https://example.com/4)\n   Plain gloss with no tail.\n": "Plain gloss with no tail.",
        }
        for body, expected in shapes.items():
            with self.subTest(body=body):
                self.assertEqual(rc.extract_items(body, 5)[0]["note"], expected)

    def test_feed_note_gap_names_missing_and_non_chinese_notes(self):
        self.assertIsNone(rr.feed_note_gap(self.manifest))
        items = self.manifest["lanes"][0]["sources"][0]["items"]
        items[:] = [
            {"title": "A", "url": "https://example.com/a", "note": "English only."},
            {"title": "B", "url": "https://example.com/b"},
            {"title": "C", "url": "https://example.com/c"},
            {"title": "D", "url": "https://example.com/d"},
        ]
        gap = rr.feed_note_gap(self.manifest)
        self.assertIn("科技动态 4 条中 1 条有摘要, 0 条为中文", gap)
        document = rr.render(self.manifest, {"schema": 1})
        self.assertIn("科技动态 4 条中 1 条有摘要", document)
        self.assertGreater(document.index("科技动态 4 条中"), document.index("以上 "))
        # Any item without a Chinese note is reported; the spec promises one
        # per item, and a tolerance would hide the partial failure.
        for item, note in zip(items, ("中文一。", "English two.", "English three.", "English four.")):
            item["note"] = note
        self.assertIn("4 条中 4 条有摘要, 1 条为中文", rr.feed_note_gap(self.manifest))
        items[1]["note"] = "中文二。"
        self.assertIn("4 条中 4 条有摘要, 2 条为中文", rr.feed_note_gap(self.manifest))
        items[2]["note"] = "中文三。"
        items[3]["note"] = "中文四。"
        self.assertIsNone(rr.feed_note_gap(self.manifest))
        del items[3]["note"]
        self.assertIn("4 条中 3 条有摘要, 3 条为中文", rr.feed_note_gap(self.manifest))

    def test_check_flags_a_feed_of_bare_headlines(self):
        for item in self.manifest["lanes"][0]["sources"][0]["items"]:
            item.pop("note")
        overview = {
            "schema": 1,
            "deep_read": {
                "total": 1,
                "entries": [{"title": "curated", "facts": ["fact"], "why": "why"}],
            },
        }
        html = rr.render(self.manifest, overview)
        self.assertEqual(len(ParsedDocument(html).root.find_all("tr", class_name="feed-item")), 2)
        findings = rr.check_html(html)
        self.assertEqual(len(findings), 1)
        self.assertIn("2 条中 0 条有摘要", findings[0])

    def test_check_command_reads_an_artifact_and_fails_on_findings(self):
        bad = Path(self.tmp.name) / "bad.html"
        valid = self._brief_document(
            {"text": "x", "label": "Hotel credit", "days_left": 42}
        )
        mutated = valid.replace(">Hotel credit<", ">Hotel credit · 42d<", 1)
        self.assertNotEqual(mutated, valid)
        bad.write_text(mutated, encoding="utf-8")
        good = Path(self.tmp.name) / "good.html"
        good.write_text(valid, encoding="utf-8")
        (self.vault / "inbox" / "digest").mkdir()
        cases = (
            ("bad content", ("--html", str(bad)), rd.CHECK_FINDINGS_EXIT, "stdout", "check: 倒计时「42d」"),
            ("missing file", ("--html", str(bad.with_name("absent.html"))), 1, "stderr", "unreadable"),
            ("empty directory", (), 1, "stderr", "no *-digest.html"),
            ("valid artifact", ("--html", str(good)), 0, "stdout", "0 finding(s)"),
        )
        for name, args, code, stream, message in cases:
            with self.subTest(case=name):
                proc = self._run("check", *args)
                self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
                self.assertIn(message, getattr(proc, stream))

    def test_check_without_html_takes_the_newest_artifact_by_mtime(self):
        """A name sort would rank the same day's weekly above the daily and
        miss an older file rendered again; modification time is the truth."""
        digests = self.vault / "inbox" / "digest"
        digests.mkdir()
        good = self._brief_document({"text": "x", "label": "Hotel credit", "days_left": 42})
        bad = good.replace(">Hotel credit<", ">Hotel credit · 42d<", 1)
        self.assertNotEqual(bad, good)
        (digests / "2099-01-31-weekly-digest.html").write_text(good, encoding="utf-8")
        older = digests / "2099-01-30-daily-digest.html"
        older.write_text(bad, encoding="utf-8")
        stamp = time.time() + 60
        os.utime(older, (stamp, stamp))
        proc = self._run("check")
        self.assertEqual(proc.returncode, rd.CHECK_FINDINGS_EXIT, proc.stdout + proc.stderr)
        self.assertIn("2099-01-30-daily-digest.html", proc.stdout)


if __name__ == "__main__":
    unittest.main()
