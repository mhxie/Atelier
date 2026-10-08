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
import html
import io
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Event
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
REPRESENTATIVE_FIXTURE = REPO_ROOT / "tests/fixtures/digest/representative.json"

import _paths  # noqa: E402
import digest_note as dn  # noqa: E402
import routine_digest as rd  # noqa: E402
import routine_collect as rc  # noqa: E402
import routine_digest_core as core  # noqa: E402
from _reflect import _CODE_SPAN_RE, TitleIndex, nonnative  # noqa: E402


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
            section for section in overview["sections"] if section["title"] != dn.DECISION_SECTION
        ]
        overview.pop("articles", None)
        brief = None
    return manifest, overview, brief, payload["retrospect"], payload["context"]


_WIKILINK = re.compile(r"\[\[([^\[\]\n|]+)(?:\|[^\[\]\n]*)?\]\]")
_THEMATIC_BREAK = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*")
# What may not open the text of an item, the headline quote, or a paragraph: a nested
# list or task, an ordered list, a heading, a quote, HTML, a fence, display math, or a
# link definition (`[ref]: x` is consumed, so its item renders empty).
# Backticks open a fence only when no backtick follows; a closed run is a code span.
_BLOCK_START = re.compile(
    r"(?:[-*+]|\d{1,9}[.)]|#{1,6})(?:[ \t]|$)|[><]|`{3,}(?!.*`)|~~~|\$\$|\[[ xX~/]\](?:\s|$)|\[[^\]\n]*\]:"
)


def h2s(note: str) -> list[str]:
    """The note's H2 titles, in order."""
    return [line[3:] for line in note.split("\n") if line.startswith("## ")]


def assert_native(note: str, titles: TitleIndex | None = None) -> None:
    """A structural stand-in for meowdown, which cannot run here.

    `nonnative` finds nothing; frontmatter, a blank line, then the only H1. No
    H3+, HTML, fence, `+ ` or task line; no item or paragraph whose text opens
    another block; only the generated quota table may span adjacent pipe rows.
    One physical line per paragraph: below the
    frontmatter every line is a heading, a `- ` or `  - ` item, or a paragraph
    with blank lines on both sides. Exactly four `---` lines (two frontmatter
    fences, the fold, the colophon) and no other rule. Every `[[` outside a code
    span starts an [[X]] that opens exactly one note in `titles`.
    """
    problems = []
    if (found := nonnative(note)) != Counter():
        problems.append(f"non-native syntax {dict(found)}")
    lines = note.split("\n")
    close = lines.index("---", 1) if lines[0] == "---" and "---" in lines[1:] else 0
    if not close or lines[close + 1:close + 2] != [""] or not "".join(lines[close + 2:close + 3]).startswith("# "):
        problems.append("the note must open with frontmatter, a blank line, then its H1")
    if (count := sum(line.startswith("# ") for line in lines)) != 1:
        problems.append(f"{count} H1 lines")
    if (count := lines.count("---")) != 4:
        problems.append(f"{count} '---' lines; expected two fences, the fold and the colophon")
    table_lines = set()
    for block in note.split("\n\n"):
        if block.startswith("| 额度 |"):
            headers, rows, rejected = rc._markdown_table("## Quota\n" + block, "Quota")
            if len(headers) < 2 or rejected or len(rows) != 1 or len(block.splitlines()) != 3:
                problems.append("malformed quota table")
            table_lines.update(block.splitlines())
    for number, line in enumerate(lines):
        bare = line.lstrip()
        content = re.sub(r"^(?:- |> )", "", bare)
        lone = not lines[number - 1].strip() and not "".join(lines[number + 1:number + 2]).strip()
        if (line.startswith("###") or bare.startswith(("+ ", "<", "```", "~~~"))
                or (not re.match(r"#{1,2} ", line) and _BLOCK_START.match(content))
                or (bare.startswith("|") and not lone and line not in table_lines)
                or (line != "---" and _THEMATIC_BREAK.fullmatch(line))):
            problems.append(f"line {number + 1} is not native: {line[:60]!r}")
        if number > close and line and not lone and line not in table_lines and not line.startswith(("# ", "## ", "- ", "  - ")):
            problems.append(f"line {number + 1} continues another block: {line[:60]!r}")
        text = _CODE_SPAN_RE.sub("", line)
        for target in _WIKILINK.findall(text):
            if titles is None or titles.resolve(target) is None:
                problems.append(f"[[{target}]] does not open exactly one note")
        if text.count("[[") != len(_WIKILINK.findall(text)):
            problems.append(f"line {number + 1} has a stray [[ that a later ]] could close: {line[:60]!r}")
    if problems:
        raise AssertionError("; ".join(problems))


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
        self.assertIsNone(dn.feed_note_gap(manifest))
        overview = {"schema": 1, "deep_read": {"total": 1, "entries": [
            {"title": "精选", "lane": "Finance", "facts": ["事实"], "why": "关联"},
        ]}}
        document = dn.render(manifest, overview)
        index = document.split("## 来源索引", 1)[1]
        self.assertIn("降级 · 0/5", index)
        self.assertNotIn("输入缺口：完整正文不可用。", document)
        source["meta"]["status"] = "complete"
        self.assertNotIn("status=complete", dn.render(manifest, overview))

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

        rd.write(self.vault, dn.render(manifest), manifest)
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
        rd.write(self.vault, dn.render(manifest), manifest)
        self.assertEqual(rc.collect(self.vault, mode="daily", until="2099-02-01")["updates"], [])

    def test_a_stale_same_day_manifest_never_replaces_a_newer_note(self):
        ledger = self.vault / "personal/status-tracker.md"
        rows = UPDATE_LEDGER.splitlines(keepends=True)
        ledger.write_text("".join(rows[:-1]))
        backdated = rc.collect(self.vault, mode="daily", until="2099-01-30")
        older = {**rc.collect(self.vault, mode="daily", until="2099-01-31"), "generated": "2099-01-31T06:20:00"}
        ledger.write_text(UPDATE_LEDGER)
        newer = {**rc.collect(self.vault, mode="daily", until="2099-01-31"), "generated": "2099-01-31T09:00:00"}
        self.assertEqual((len(backdated["updates"]), len(older["updates"]), len(newer["updates"])), (1, 1, 2))
        target = core.note_path(self.vault, newer)
        state = self.vault / rc.DIGEST_UPDATES_STATE
        self.assertEqual(rd.write(self.vault, dn.render(newer), newer), 0)
        note, before = target.read_bytes(), state.read_bytes()
        # Even the approval hash of the current note cannot let an older collection replace it.
        for replace in ("", rd._sha(note)):
            with self.subTest(replace=bool(replace)), self.assertRaisesRegex(SystemExit, "newer collection"):
                rd.write(self.vault, dn.render(older), older, replace=replace)
            self.assertEqual(target.read_bytes(), note)
            self.assertEqual(state.read_bytes(), before)
        preview = Path(self.tmp.name) / "historical.md"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rd.write(self.vault, dn.render(older), older, out=preview), 0)
        self.assertTrue(preview.is_file())
        self.assertEqual(state.read_bytes(), before)
        # Writing day D after day D+1 creates D's note but never moves D+1's cursor back.
        self.assertEqual(rd.write(self.vault, dn.render(backdated), backdated), 0)
        self.assertTrue(core.note_path(self.vault, backdated).is_file())
        cursor = json.loads(state.read_text(encoding="utf-8"))["daily"]["status-ledger"]
        self.assertEqual(cursor, newer["updates"][-1]["id"])
        self.assertEqual(rc.collect(self.vault, mode="daily", until="2099-02-01")["updates"], [])

    def test_same_day_recollect_replays_the_days_updates(self):
        ledger = self.vault / "personal/status-tracker.md"
        rows = UPDATE_LEDGER.splitlines(keepends=True)
        ledger.write_text("".join(rows[:-1]).replace("2099-01-30", "2099-01-29"))
        first = rc.collect(self.vault, mode="daily", until="2099-01-29")
        self.assertEqual(rd.write(self.vault, dn.render(first), first), 0)
        older = {**rc.collect(self.vault, mode="daily", until="2099-01-30"), "generated": "2099-01-30T06:20:00"}
        self.assertEqual(older["updates"], [])
        ledger.write_text(ledger.read_text() + rows[-1])
        newer = {**rc.collect(self.vault, mode="daily", until="2099-01-30"), "generated": "2099-01-30T09:00:00"}
        self.assertEqual(len(newer["updates"]), 1)
        self.assertEqual(rd.write(self.vault, dn.render(newer), newer), 0)
        target = core.note_path(self.vault, newer)
        state = self.vault / rc.DIGEST_UPDATES_STATE
        note, before = target.read_bytes(), state.read_bytes()
        # The empty manifest collected before the row landed is older, so it cannot blank the note.
        with self.assertRaisesRegex(SystemExit, "newer collection"):
            rd.write(self.vault, dn.render(older), older)
        self.assertEqual((target.read_bytes(), state.read_bytes()), (note, before))
        # A same-day recollect replays the day's rows, so its render is the same bytes.
        fresh = {**rc.collect(self.vault, mode="daily", until="2099-01-30"), "generated": newer["generated"]}
        self.assertEqual([u["id"] for u in fresh["updates"]], [u["id"] for u in newer["updates"]])
        modified = target.stat().st_mtime_ns
        self.assertEqual(rd.write(self.vault, dn.render(fresh), fresh), 0)
        self.assertEqual(target.read_bytes(), note)
        self.assertEqual(target.stat().st_mtime_ns, modified)
        self.assertEqual(rc.collect(self.vault, mode="daily", until="2099-01-31")["updates"], [])

    def test_unreadable_update_configuration_blocks_publication(self):
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-30")
        self.assertEqual(rd.write(self.vault, dn.render(manifest), manifest), 0)
        state = self.vault / rc.DIGEST_UPDATES_STATE
        target = core.note_path(self.vault, manifest)
        before, note = state.read_bytes(), target.read_bytes()
        config = self.vault / rc.DIGEST_UPDATES_CONFIG
        for updates in (manifest["updates"], []):
            with self.subTest(updates=bool(updates)):
                config.write_text("[broken")
                candidate = {**manifest, "updates": updates}
                with self.assertRaisesRegex(SystemExit, "config unreadable"):
                    rd.write(self.vault, dn.render(candidate, {"schema": 1}), candidate)
                self.assertEqual(target.read_bytes(), note)
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
        # The second writer reads the state only after the first released the lock, so both records survive.
        self.assertEqual(set(state["notes"]), {"inbox/digest/2099-01/2099-01-25-daily-digest.md",
                                               "inbox/digest/2099-01/2099-01-30-daily-digest.md"})
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
        rd.write(self.vault, dn.render(manifest), manifest)

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
        rd.write(self.vault, dn.render(daily), daily)

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
        rd.write(self.vault, dn.render(first), first)
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
        rd.write(self.vault, dn.render(first), first)
        later = rc.collect(self.vault, mode="daily", until="2099-01-31")
        rd.write(self.vault, dn.render(later), later)
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

    def test_a_row_with_an_absolute_output_dir_is_still_collected(self):
        """HEAD globbed `ov / output_dir`, which pathlib resolves to an absolute row, and kept the file."""
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "2099-01-30-report.md").write_text("# Report\n\n- A synthetic finding.\n", encoding="utf-8")
        registry = self.vault / "_tools/routines/registry.toml"
        registry.write_text(registry.read_text() + '\n[[routine]]\nname = "outside-report"\n'
                            f'output_dir = {json.dumps(str(outside))}\nfile_pattern = "*-report.md"\n',
                            encoding="utf-8")
        manifest = rc.collect(self.vault, mode="weekly", until="2099-01-30")
        self.assertIn("2099-01-30-report.md", [source["name"] for _, source in core.iter_sources(manifest)])


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
        entry = f"`{ref['path']}`"

        def index_entries(document: str) -> list[str]:
            index = document.split("## 来源索引", 1)[1].split("\n---\n", 1)[0]
            return [line for line in index.splitlines() if line.startswith("- ") and entry in line]

        document = dn.render(manifest, overview)
        assert_native(document)
        background = document.split("**背景来源**\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(background, f"- **＜script>background＜/script>** · {entry} · 2099-01-20")
        self.assertIn(f"- A cited background fact · {entry}", document)
        self.assertNotIn("unmatched", document)
        for live in ("<script", "<img"):
            self.assertNotIn(live, document)
        self.assertEqual(len(index_entries(document)), 1)
        self.assertGreater(document.index("missing source"), document.index("以上 "))
        manifest["context_sources"]["duplicate"] = dict(ref)
        self.assertEqual(len(index_entries(dn.render(manifest, overview))), 1)
        manifest["lanes"] = [{"lane": "Research", "files": 1, "sources": [dict(ref)]}]
        document = dn.render(manifest, overview)
        self.assertEqual(len(index_entries(document)), 1)
        self.assertNotIn("**背景来源**", document)


class RenderTests(VaultCase):
    def test_title_shapes(self):
        self.assertEqual(dn.digest_title(self.manifest), "Atelier Weekly: 2099-01-24 → 2099-01-30")
        daily = rc.collect(self.vault, mode="daily", until="2099-01-30")
        self.assertEqual(dn.digest_title(daily), "Atelier Daily: 2099-01-30")
        backlog = rc.collect(self.vault, mode="weekly", until="2099-01-30", unacked=True)
        self.assertEqual(dn.digest_title(backlog), "Atelier Digest: backlog through 2099-01-30")
        for manifest in (self.manifest, daily, backlog):
            self.assertIn(f"\n\n# {dn.digest_title(manifest)}\n\n", dn.render(manifest))

    def test_carried_source_is_labelled_in_the_index(self):
        manifest = rc.collect(self.vault, mode="daily", until="2099-01-31")
        self.assertIn(" · 补录 · ", dn.render(manifest).split("## 来源索引", 1)[1])
        self.assertNotIn("补录", dn.render(self.manifest))

    def test_gaps_render_in_the_colophon_never_as_a_section(self):
        overview = {"schema": 1, "headline": "h", "sections": [], "gaps": ["Readwise CLI unavailable"]}
        document = dn.render(self.manifest, overview)
        fold = document.index("以上 ")
        self.assertGreater(document.index("- Readwise CLI unavailable"), fold)
        self.assertGreater(document.index("**输入缺口**"), fold)
        self.assertFalse([title for title in h2s(document) if "输入缺口" in title])
        quiet = {**self.manifest, "lanes": [lane for lane in self.manifest["lanes"] if lane["lane"] != "Tech feed"]}
        self.assertNotIn("输入缺口", dn.render(quiet, {"schema": 1, "headline": "h", "sections": []}))

    def test_brief_renders_above_the_overview(self):
        """The action surface is the first screen, ahead of the intel overview."""
        brief = self._brief(
            {"text": "Hotel credit 明天", "source": "finance/example-tracker.md:107"},
            kind="closing_now", heading="今天/明天关窗 1 件",
        )
        brief["warnings"] = ["deadline index stale 12d"]
        overview = {"schema": 1, "sections": [{"title": "情报", "bullets": [{"text": "x"}]}]}
        document = dn.render(self.manifest, overview, brief)
        self.assertIn("## 今天/明天关窗 1 件", document)
        self.assertIn("- Hotel credit 明天 · `example-tracker:107`", document)
        self.assertIn("\n\n! deadline index stale 12d\n\n", document)
        self.assertLess(document.index("今天/明天关窗"), document.index("## 情报"))
        self.assertLess(document.index("## 情报"), document.index("## 来源索引"))

    def test_brief_item_prints_once_ahead_of_folded_debt(self):
        """Actions and folded reminders have Outline targets; each item appears once."""
        brief = self._brief(
            {"text": "a forfeitable thing", "source": "finance/x.md:1"},
            kind="closing", heading="需要开始处理 1 件",
        )
        brief["groups"].append(
            {"tier": 3, "kind": "review", "heading": "review 债 2 项", "folded": True, "items": []}
        )
        brief["warnings"] = ["deadline index missing"]
        document = dn.render(self.manifest, {}, brief)
        assert_native(document)
        self.assertEqual(document.count("a forfeitable thing"), 1)
        self.assertIn("需要开始处理 1 件", h2s(document))
        self.assertIn("其他提醒", h2s(document))
        self.assertLess(document.index("a forfeitable thing"), document.index("review 债 2 项"))
        self.assertEqual(document.count("! deadline index missing"), 1)

    def test_optional_inputs_keep_source_navigation_and_exclusion_disclosure(self):
        document = dn.render(self.manifest)
        self.assertNotIn("## 今日", document)
        self.assertIn("\n\n" + dn.NO_OVERVIEW + "\n\n", document)
        self.assertIn("## 来源索引", document)
        self.assertIn("**daily feed digest**", document)
        self.assertIn("[First item title](https://example.com/one)", document)
        self.assertIn("`inbox/feed/2099-01-30-feed.md`", document)
        self.assertIn("Excluded from this digest", document)
        self.assertIn("maintenance output", document)

    def test_configured_updates_render_before_the_overview(self):
        overview = {"schema": 1, "sections": [{"title": "情报", "bullets": [{"text": "x"}]}]}
        document = dn.render(self.manifest, overview)
        self.assertIn("## 状态更新 · 2", document)
        self.assertIn("- **Status ledger** · 2099-01-30 · `personal/status-tracker.md`", document)
        self.assertIn("  - Cutoff: 2098-06-01", document)
        self.assertIn("  - Sources: [Primary](https://example.com/status)", document)
        self.assertLess(document.index("## 状态更新"), document.index("## 情报"))

    def test_folded_brief_group_renders_heading_only(self):
        brief = self._brief(tier=3, kind="recurring", heading="recurring: 9 条逾期 (recurring.py list)", folded=True)
        brief["groups"].append({"heading": "体重上次 2099-01-28 (2d 前)", "items": []})
        document = dn.render(self.manifest, None, brief)
        ledger = document.split("## 其他提醒\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(ledger, "- recurring: 9 条逾期\n- 体重上次 2099-01-28 (2d 前)")
        self.assertNotIn("recurring.py list", document)

    def test_unknown_input_schemas_are_rejected(self):
        path = Path(self.tmp.name) / "unknown.json"
        path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
        for loader in (rc.load_brief, rc.load_overview, rc.load_context):
            with self.subTest(loader=loader.__name__), self.assertRaises(SystemExit):
                loader(path)

    def test_brief_text_is_escaped(self):
        brief = self._brief({"text": "<img onerror=x>"}, kind="closing_now", heading="<script>h</script>")
        brief["warnings"] = ["<b>w</b>"]
        document = dn.render(self.manifest, None, brief)
        for live in ("<script", "<img", "<b>"):
            self.assertNotIn(live, document)
        self.assertIn("## ＜script>h＜/script>", document)
        self.assertIn("- ＜img onerror=x>", document)
        self.assertIn("! ＜b>w＜/b>", document)
        assert_native(document)

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

    def test_overview_has_real_links_and_title_or_path_provenance(self):
        source_path = "inbox/feed/2099-01-30-feed.md"
        overview = self._overview_with_source(source_path)
        document = dn.render(self.manifest, overview)
        self.assertIn(f"- A **bold** claim with a [link](https://example.com/z) · `{source_path}`", document)
        titles = TitleIndex(self.vault)
        title = titles.link_title(Path(source_path))
        self.assertTrue(title)
        linked = dn.render(self.manifest, overview, titles=titles)
        self.assertIn(f"- A **bold** claim with a [link](https://example.com/z) · [[{title}]]", linked)
        assert_native(linked, titles)

    def test_a_source_outside_the_manifest_is_marked_unmatched(self):
        (self.vault / "finance/invented.md").write_text("# An Invented Note\n", encoding="utf-8")
        overview = self._overview_with_source("finance/invented.md")
        for titles in (None, TitleIndex(self.vault)):
            with self.subTest(titles=titles is not None):
                document = dn.render(self.manifest, overview, titles=titles)
                self.assertIn(" · `invented.md` (unmatched)", document)
                self.assertNotIn("[[An Invented Note]]", document)
                self.assertNotIn("`finance/invented.md`", document)

    def test_overview_html_is_inert(self):
        overview = {
            "schema": 1,
            "sections": [
                {"title": "T", "bullets": [{"text": "<script>alert(1)</script> and <b>x</b>"}]}
            ],
        }
        document = dn.render(self.manifest, overview)
        self.assertNotIn("<script", document)
        self.assertNotIn("<b>", document)
        self.assertIn("- ＜script>alert(1)＜/script> and ＜b>x＜/b>", document)
        assert_native(document)

    def test_headline_section_note_and_bullet_link_keep_their_places(self):
        """The headline quote leads the overview below 状态更新; a note heads its section; only an http url links."""
        cited = "inbox/feed/2099-01-30-feed.md"
        overview = {"schema": 1, "headline": "今日一句", "sections": [{"title": "信号", "note": "A **note**", "bullets": [
            {"text": "x", "url": "https://example.com/s", "sources": [cited]},
            {"text": "y", "url": "javascript:alert(1)", "sources": [cited]}]}]}
        document = dn.render(self.manifest, overview)
        self.assertIn("\n\n> 今日一句\n\n## 信号 · 2\n\nA **note**\n\n"
                      f"- x · [来源](https://example.com/s) · `{cited}`\n- y · `{cited}`\n\n", document)
        self.assertLess(document.index("## 状态更新"), document.index("> 今日一句"))
        assert_native(document)

    def test_a_one_day_window_is_titled_daily_whatever_the_mode(self):
        weekly = {**self.manifest, "window": {"since": "2099-01-30", "until": "2099-01-30"}}
        self.assertEqual(dn.digest_title(weekly), "Atelier Daily: 2099-01-30")


class RepresentativeDigestTests(unittest.TestCase):
    """One shareable fixture pins the whole rendered information hierarchy."""

    def assert_text_order(self, text: str, *needles: str) -> None:
        offsets = [text.index(needle) for needle in needles]
        self.assertEqual(offsets, sorted(offsets), needles)

    def test_daily_fixture_preserves_content_and_visual_order(self):
        document = dn.render(*representative_digest_inputs("daily"))
        assert_native(document)
        self.assert_text_order(
            document,
            "# Atelier Daily",
            "需要开始处理 1 件",
            "本季主线",
            "TODO 到期 1 件",
            "## 状态更新",
            "## 需要的决策",
            "## 前沿实验室",
            "## routine 摘要",
            "## 新文章",
            "以上 ",
            "## 信号精选",
            "## 随机回顾",
            "## 来源索引",
            "**输入缺口**",
            "KB 源文本",
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
            "[来源 ↗](https://example.com/research)",
        ):
            self.assertIn(visible, document)
        for hidden in (
            "A bare title that must be dropped",
            "第三条应被深度上限裁掉。",
            "第五行不应出现",
            "An unreviewed item that must stay hidden",
            "## 输入缺口",
        ):
            self.assertNotIn(hidden, document)
        card = document.split("## 需要的决策 · 1\n\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(len(card), 4)
        for line, prefix in zip(card, ("- 是否验证", "  - A. ", "  - B. ", "  - 定案：")):
            self.assertTrue(line.startswith(prefix), line)

    def test_weekly_fixture_removes_daily_actions_but_keeps_intel_order(self):
        document = dn.render(*representative_digest_inputs("weekly"))
        assert_native(document)
        self.assertIn("# Atelier Weekly: 2099-01-24 → 2099-01-30", document)
        for daily_only in (
            "需要开始处理 1 件",
            "本季主线",
            "TODO 到期 1 件",
            "需要的决策",
            "A useful saved article",
        ):
            self.assertNotIn(daily_only, document)
        self.assert_text_order(
            document,
            "## 状态更新",
            "## 信号",
            "## 前沿实验室",
            "## routine 摘要",
            "以上 ",
            "## 信号精选",
            "## 随机回顾",
            "## 来源索引",
        )

    def test_the_colophon_leaves_implementation_paths_out_of_the_note(self):
        document = dn.render(*representative_digest_inputs("daily"))
        self.assertNotIn("<paths.", document)
        self.assertNotIn("registry.toml", document)
        self.assertNotIn("digest_updates.toml", document)


class WriteTests(VaultCase):
    def test_overdue_todos_stop_after_three_daily_notes(self):
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
                manifest = {"mode": "daily", "window": {"until": today.isoformat()},
                            "generated": f"{today.isoformat()}T06:20:00"}
                before = state.read_bytes() if state.exists() else None
                # Previews and dry runs record nothing, so they never spend a reminder.
                with contextlib.redirect_stdout(io.StringIO()):
                    rd.write(self.vault, "# digest\n", manifest, out=Path(self.tmp.name) / f"day-{number}.md",
                             brief=brief)
                    rd.write(self.vault, "# digest\n", manifest, brief=brief, dry_run=True)
                self.assertEqual(state.read_bytes() if state.exists() else None, before)
                with patch.object(rd, "atomic_write", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        rd.write(self.vault, "# digest\n", manifest, brief=brief)
                self.assertEqual(state.read_bytes() if state.exists() else None, before)
                self.assertFalse(core.note_path(self.vault, manifest).exists())
                for _ in range(2):
                    self.assertEqual(rd.write(self.vault, "# digest\n", manifest, brief=brief), 0)
                shown = rc.load_todo_reminders(self.vault)[item["reminder_id"]]
                self.assertEqual(len(shown), number)
                todo.line += 1
                self.assertEqual(db.load_todos(self.vault, today, [])[0].items[0].reminder_id,
                                 item["reminder_id"])
            # A previously collected brief cannot bypass the limit on a later day.
            brief["date"] = manifest["window"]["until"] = "2099-01-24"
            with self.assertRaisesRegex(SystemExit, "limit reached"):
                rd.write(self.vault, "# digest\n", manifest, brief=brief)
            self.assertFalse(core.note_path(self.vault, manifest).exists())
            self.assertEqual(rd.write(self.vault, "# weekly\n", self.manifest, brief=brief), 0)
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

    def test_writer_refusals(self):
        for name, manifest in (
            ("unknown mode", {**self.manifest, "mode": "monthly"}),
            ("missing until", {**self.manifest, "window": {"since": "2099-01-24"}}),
            ("malformed until", {**self.manifest, "window": {"until": "2099-1-30"}}),
        ):
            with self.subTest(case=name), self.assertRaisesRegex(SystemExit, "YYYY-MM-DD"):
                rd.write(self.vault, "# x\n", manifest)
        with self.assertRaisesRegex(SystemExit, "outside"):
            rd.write(self.vault, "# x\n", self.manifest, out=self.vault / "inbox/preview.md")
        self.assertFalse((self.vault / "inbox/preview.md").exists())
        self.assertFalse((self.vault / "inbox/digest").exists())
        self.assertFalse((self.vault / rc.DIGEST_UPDATES_STATE).exists())
        # A note that is not UTF-8 text is never replaced, and no approval hash is offered.
        target = core.note_path(self.vault, self.manifest)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\xff\xfe not text")
        manifest_path = Path(self.tmp.name) / "weekly.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        proc = self._run("write", "--manifest", str(manifest_path))
        self.assertEqual(proc.returncode, rd.REFUSED_EXIT, proc.stdout + proc.stderr)
        self.assertIn("is not UTF-8 text; inspect it or move it aside, then write again", proc.stderr)
        self.assertNotIn("--replace", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(target.read_bytes(), b"\xff\xfe not text")
        self.assertFalse((self.vault / rc.DIGEST_UPDATES_STATE).exists())

    def test_dry_run_reports_the_path_without_writing(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = rd.write(
                self.vault,
                "# x\n",
                self.manifest,
                dry_run=True,
            )
        self.assertEqual(code, 0)
        self.assertIn("would write $OV/inbox/digest/2099-01/2099-01-30-weekly-digest.md", buffer.getvalue())
        self.assertIn("sha256 " + rd._sha("# x\n"), buffer.getvalue())
        self.assertFalse((self.vault / "inbox" / "digest").exists())
        self.assertFalse((self.vault / rc.DIGEST_UPDATES_STATE).exists())

    def test_only_an_overdue_todo_spends_an_edition(self):
        """daily_brief also lists TODOs due today or tomorrow; the three-edition cap counts overdue ones only."""
        manifest = {"mode": "daily", "window": {"until": "2099-01-30"}, "generated": "2099-01-30T06:20:00"}
        brief = self._brief({"text": "due today", "days_left": 0, "reminder_id": "r-today"},
                            {"text": "overdue", "days_left": -1, "reminder_id": "r-late"}, kind="todo_now")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rd.write(self.vault, "# digest\n", manifest, brief=brief), 0)
        self.assertEqual(rc.load_todo_reminders(self.vault), {"r-late": ["2099-01-30"]})

    def test_an_unreadable_title_index_falls_back_to_path_citations(self):
        err = io.StringIO()
        with patch.object(rd, "TitleIndex", side_effect=OSError(5, "Input/output error")), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(rd._titles(self.vault))
        self.assertIn("warning: title index unavailable (5); notes cited by path", err.getvalue())


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
        written = self.vault / "inbox/digest/2020-01/2020-01-01-daily-digest.md"
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
            "write", "--manifest", str(manifest_path)
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
            "write", "--manifest", str(manifest_path)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = self.vault / "inbox" / "digest" / "2099-01" / "2099-01-31-daily-digest.md"
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
                     ("render", "--manifest", "missing.json"),
                     ("mail", "--html", "x", "--subject", "y"), ("check",),
                     ("write", "--manifest", "m", "--routine", "x")):
            with self.subTest(args=args):
                proc = self._run(*args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("usage:", proc.stderr)

    def test_missing_registry_fails_loudly(self):
        (self.vault / "_tools/routines/registry.toml").unlink()
        proc = self._run("collect", "--json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("routine registry missing", proc.stderr)

    def test_write_names_a_research_lane_gap_on_stderr(self):
        manifest, *_ = representative_digest_inputs("daily")
        overview = {"schema": 1, "deep_read": {"entries": [
            {"title": "A finance pick", "lane": "Finance", "facts": ["fact"]}]}}
        paths = {}
        for name, data in (("manifest", manifest), ("overview", overview)):
            paths[name] = Path(self.tmp.name) / f"{name}.json"
            paths[name].write_text(json.dumps(data), encoding="utf-8")
        proc = self._run("write", "--manifest", str(paths["manifest"]), "--overview", str(paths["overview"]),
                         "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warning: 情报精选没有 Research 条目", proc.stderr)
        self.assertFalse((self.vault / "inbox/digest").exists())


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

    def test_scan_depth_and_reviewed_recall_share_one_priced_note(self):
        document = dn.render(self.manifest, self.overview, None, self.picks)
        fold = document.index("以上 ")
        self.assertRegex(document, r"\n---\n\n以上 \d+ 分钟读完 · 以下 \d+ 分钟，按需\n")
        self.assertLess(document.index("## 信号"), fold)
        for heading in ("## 科技动态", "## 随机回顾", "## 来源索引"):
            with self.subTest(heading=heading):
                self.assertGreater(document.index(heading), fold)
        for heading in ("## 科技动态", "## 随机回顾"):
            self.assertRegex(document[document.index(heading):].split("\n", 1)[0], r" · \d+ 分钟$")
        self.assertIn("- **An Old Idea** · 1.1 年前 · wiki · `wiki/Old.md`\n  - 旧笔记的正文摘录。", document)
        self.assertNotIn("must never appear", document)
        self.assertNotIn("Sensitive", document)
        # Depth is curated picks and the feed's own items; a routine body never renders.
        self.assertNotIn("a tighter path raises discount rates", document)

    def test_the_fold_prices_the_scan_above_it_and_the_depth_below_it(self):
        manifest, overview, brief, picks, context = representative_digest_inputs("daily")
        overview["headline"] = "字" * 1980
        picks[0]["excerpt"] = "字" * 990
        document = dn.render(manifest, overview, brief, picks, context)
        fold = re.search(r"\n\n---\n\n以上 (\d+) 分钟读完 · 以下 (\d+) 分钟，按需\n\n", document)
        scan = document[document.index("\n# ") + 1:fold.start()].split("\n", 1)[1]
        prices = (dn.reading_minutes(scan), dn.reading_minutes(document[fold.end():]))
        self.assertEqual((int(fold[1]), int(fold[2])), prices)
        self.assertEqual(len(set(prices + (dn.reading_minutes(scan + "\n" + document[fold.end():]),))), 3)


class ReadingCostTests(unittest.TestCase):
    def test_empty_text_costs_nothing(self):
        self.assertEqual(dn.reading_minutes(""), 0)
        self.assertEqual(dn.reading_minutes("---\n\n## · **`x`**\n\n- [](https://example.com/a/b)"), 0)

    def test_any_real_text_costs_at_least_a_minute(self):
        """Rounding to zero would read as "free", which no section is."""
        self.assertEqual(dn.reading_minutes("**短**"), 1)

    def test_markup_is_not_counted_as_prose(self):
        # 822 characters price at 2.49 minutes, so three counted tokens of syntax would round to 3.
        bare = dn.reading_minutes("字" * 822)
        marked = dn.reading_minutes(
            f"## {'字' * 411}\n\n- **{'字' * 411}** · `one two three` · "
            "[](https://a.example/x) [](https://b.example/y) [](https://c.example/z)"
        )
        self.assertEqual((bare, marked), (2, 2))

    def test_cjk_and_latin_are_priced_separately(self):
        self.assertGreater(dn.reading_minutes("字" * 1000), 1)
        self.assertGreaterEqual(dn.reading_minutes(" ".join(["word"] * 700)), 3)


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
        document = dn.render(self.manifest, {"schema": 1, "articles": [self._one()]})
        self.assertIn("## 新文章 · 1 篇 · 13 分钟", document)
        self.assertIn("- [A Paper](https://read.readwise.io/read/abc) · 13 mins · example.com", document)
        self.assertNotIn("13 mins min", document)
        self.assertIn("  - 为何读：服务 multimodal 方向", document)
        self.assertIn("  - 这篇讲的是把长视频按 GOP 分块存储并加帧级索引。", document)
        self.assertLess(document.index("A Paper"), document.index("以上 "))

    def test_article_durations_keep_units_and_do_not_total_unknown_values(self):
        for value, label, minutes in (
            (13, "13 min", 13), ("13", "13 min", 13),
            ("1 hr 5 mins", "1 hr 5 mins", 65), ("2 hrs", "2 hrs", 120),
            (13.5, "13.5 min", 13.5), ("13.5", "13.5 min", 13.5),
            ("13.5 mins", "13.5 mins", 13.5), ("unknown", "unknown", None),
            (None, "", None), ("<b>x</b>", "<b>x</b>", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(dn._article_duration(value), (label, minutes))
                heading, rows = dn._articles([self._one(minutes=value, source="")])
                link = "- [A Paper](https://read.readwise.io/read/abc)"
                self.assertEqual(rows.splitlines()[0], f"{link} · {dn.plain(label)}" if label else link)
                self.assertEqual(heading, f"## 新文章 · 1 篇 · {minutes:g} 分钟" if minutes else "## 新文章 · 1 篇")
        self.assertEqual(
            dn._articles([self._one(minutes=13), self._one(minutes="unknown")])[0],
            "## 新文章 · 2 篇",
        )

    def test_incomplete_articles_are_dropped(self):
        for missing in ({"abstract": ""}, {"abstract": "   "}, {"title": ""}):
            with self.subTest(missing=missing):
                document = dn.render(
                    self.manifest, {"schema": 1, "articles": [self._one(**missing)]}
                )
                self.assertNotIn("A Paper", document)
                self.assertNotIn("## 新文章", document)

    def test_optional_fields_degrade_rather_than_break(self):
        document = dn.render(
            self.manifest,
            {"schema": 1, "articles": [self._one(url="", minutes="", source="", why="")]},
        )
        section = document.split("## 新文章 · 1 篇\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(section.splitlines(), ["- **A Paper**", "  - 这篇讲的是把长视频按 GOP 分块存储并加帧级索引。"])

    def test_article_text_cannot_inject_markup(self):
        document = dn.render(
            self.manifest,
            {"schema": 1, "articles": [self._one(title="<script>x</script>", abstract="<b>not bold</b>")]},
        )
        self.assertNotIn("<script>", document)
        self.assertNotIn("<b>not bold</b>", document)
        self.assertIn("- [＜script>x＜/script>](https://read.readwise.io/read/abc)", document)
        assert_native(document)


class MarkdownSubsetTests(unittest.TestCase):
    """Routine reports are Markdown and are data, never instruction."""

    def test_images_are_removed_not_rendered(self):
        """An image or embed in untrusted text would load in Reflect; it renders as nothing or plain text."""
        self.assertEqual(dn.md("![alt](https://example.com/x.png)\n\nreal text"), "real text")
        overview = {"schema": 1, "sections": [{"title": "信号", "bullets": [
            {"text": "![alt](https://example.com/x.png) real text ![[Embedded Note]]"}]}]}
        document = dn.render(representative_digest_inputs()[0], overview)
        self.assertNotIn("![", document)
        self.assertNotIn("example.com/x.png", document)
        self.assertIn("- real text Embedded Note", document)
        assert_native(document)

    def test_untrusted_markup_has_no_active_content_or_non_http_links(self):
        manifest = representative_digest_inputs()[0]
        for text in ('<script>alert(1)</script><img src="https://example.com/x">',
                     '[click](javascript:alert(1))', '[click](jav&#x61;script:alert(1))',
                     '[click](data:text/html,payload)', '[click](file:///etc/passwd)',
                     '[click](//example.com/path)', '[click](mailto:someone@example.com)',
                     '[click](https://example.com/\"onmouseover=\"alert(1))'):
            with self.subTest(text=text):
                overview = {"schema": 1, "sections": [{"title": "信号", "bullets": [{"text": text}]}]}
                for rendered in (dn.md(text), dn.render(manifest, overview)):
                    self.assertNotRegex(rendered, r"<(?:script|img)\b|\]\((?!https?://)")
                    self.assertNotIn("onmouseover", "".join(re.findall(r"\]\(([^)\n]*)\)", rendered)))
        self.assertEqual(dn.md("[ok](https://example.com/a_(b)?x=1)"), "[ok](https://example.com/a_%28b%29?x=1)")

    def test_ledger_cells_keep_links_and_break_lines_inline(self):
        manifest = representative_digest_inputs()[0]
        manifest["updates"][0]["values"] = {
            "Action": "See [[Ledger Row#Two|row two]] and [primary](https://example.com/status)",
            "State": "Done<br>verified<BR/>twice",
        }
        document = dn.render(manifest)
        self.assertIn("\n  - Action: See row two and [primary](https://example.com/status)\n"
                      "  - State: Done · verified · twice\n", document)
        assert_native(document)

    def test_escaped_entities_are_shown_never_decoded_twice(self):
        text = "&amp;lt;b&amp;gt; and &amp;amp;"
        self.assertEqual(dn.md(text), "＆lt;b＆gt; and ＆amp;")
        overview = {"schema": 1, "sections": [{"title": "信号", "bullets": [{"text": text}]}]}
        assert_native(dn.render(representative_digest_inputs()[0], overview))

    def test_field_shaping_rules(self):
        """Rules of `md` and `plain` that the hostile placements cannot tell apart, pinned one by one."""
        for raw, inert in (
            ("a <", "a ＜"),  # '<' at a field's end, as well as before a letter, / ! or ?
            ("<?php echo 1 ?>", "＜?php echo 1 ?>"),
            ("[ref]: https://example.com", "［ref]: https://example.com"),  # a definition would empty its item
            ("[ref]: <x>", "［ref]: ＜x>"),
            ("nul\x00 esc\x1b[31m bell\x07", "nul esc [31m bell"),
            ("line one\nline two", "line one line two"),
            ("第一行\n第二行", "第一行第二行"),
            ("[t](HTTPS://EXAMPLE.COM/t)", "[t](HTTPS://EXAMPLE.COM/t)"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(dn.md(raw), inert)
        self.assertEqual(dn.plain("see [t](https://example.com/t)"), "see t")
        self.assertEqual(dn._linked("a ] b [", "https://example.com"), "[a ］ b ［](https://example.com)")

    def test_rendering_without_titles_does_no_io(self):
        inputs = representative_digest_inputs("daily")
        _paths.tier_segments()  # the public path registry is read once, before rendering
        with patch("socket.socket", side_effect=AssertionError("rendering must be offline")), \
             patch("builtins.open", side_effect=AssertionError("rendering opens no file")), \
             patch.object(Path, "open", side_effect=AssertionError("rendering opens no note")), \
             patch.object(Path, "read_text", side_effect=AssertionError("rendering reads no note")), \
             patch("os.scandir", side_effect=AssertionError("rendering walks no folder")):
            document = dn.render(*inputs)
        self.assertIn("\n# Atelier Daily: 2099-01-30\n", document)
        self.assertNotIn("[[", document)

    def test_imports_need_no_site_packages(self):
        result = subprocess.run(
            [sys.executable, "-S", "-c", "import sys; sys.path.insert(0, 'scripts'); "
             "import routine_digest, digest_note; "
             "loaded = {'prefect', 'markdown_it'} & set(sys.modules); assert not loaded, loaded"],
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


class AttentionBudgetTests(VaultCase):
    """The renderer enforces each routine's budget, regardless of its writer."""

    def setUp(self):
        super().setUp()
        self.path = next(core.iter_sources(self.manifest))[1]["path"]

    def _render(self, lines: int, cap: int | None = None) -> str:
        if cap is not None:
            for _lane, source in core.iter_sources(self.manifest):
                source["max_lines"] = cap
        summary = "\n".join(f"第 {n} 行" for n in range(1, lines + 1))
        return dn.render(
            self.manifest,
            {"schema": 1, "routines": [{"path": self.path, "summary": summary}]},
        )

    def test_a_summary_inside_its_budget_is_untouched(self):
        document = self._render(3, cap=5)
        self.assertIn("\n  - 第 3 行\n", document)
        self.assertNotIn("已截至", document)

    def test_an_overlong_summary_is_cut_and_says_so(self):
        document = self._render(12, cap=5)
        self.assertIn("\n  - 第 5 行\n", document)
        self.assertNotIn("第 6 行", document)
        self.assertIn(" · 已截至 5 行 · ", document)

    def test_the_registry_supplies_the_cap(self):
        self.assertEqual(
            {s["label"]: s["max_lines"] for _l, s in core.iter_sources(self.manifest)}[
                "daily feed digest"
            ],
            core.DEFAULT_ROUTINE_LINES,
        )

    def test_an_empty_summary_yields_no_entry(self):
        document = dn.render(
            self.manifest,
            {"schema": 1, "routines": [{"path": self.path, "summary": "  "}]},
        )
        self.assertNotIn("routine 摘要", document)

    def test_summary_text_cannot_inject_markup(self):
        document = dn.render(
            self.manifest,
            {"schema": 1, "routines": [{"path": self.path, "summary": "<script>x</script>"}]},
        )
        self.assertNotIn("<script>", document)
        self.assertIn("\n  - ＜script>x＜/script>\n", document)
        assert_native(document)

    def test_briefs_sit_above_the_fold_and_bodies_below(self):
        overview = {"schema": 1, "routines": [{"path": self.path, "summary": "一行摘要"}]}
        document = dn.render(self.manifest, overview, None, [])
        self.assertLess(document.index("## routine 摘要"), document.index("以上 "))
        self.assertGreater(document.index("## 科技动态"), document.index("以上 "))


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

    @staticmethod
    def _strip(document: str) -> str:
        """The signal strip: the masthead paragraph of live cells, or ''."""
        scan = document.split("\n---\n\n以上 ", 1)[0]
        return next((block for block in scan.split("\n\n")
                     if re.match(r"(?:==|\*\*)?(?:关窗|主线|体重|决策|失败尝试|Prefect) ", block)), "")

    def test_weather_sits_in_the_masthead_and_provenance_in_the_colophon(self):
        document = dn.render(self.manifest, None, None, None, self.context)
        self.assertIn("\n\n**Lisbon** 13–25°C · 少云 · 降水 2% · 9:00 18° · 18:00 21°\n\n", document)
        self.assertLess(document.index("Lisbon"), document.index("## 来源索引"))
        self.assertLess(document.index("## 来源索引"), document.index("KB 源文本"))
        self.assertLess(document.index("## 来源索引"), document.index("生成于 06:22"))
        self.assertGreater(document.index("天气 Open-Meteo"), document.index("## 来源索引"))

    def test_quota_table_marks_the_remaining_share_by_level(self):
        def rows(context: dict) -> list[str]:
            document = dn.render(self.manifest, None, None, None, context)
            assert_native(document)
            return [line for line in document.splitlines() if line.startswith("| ")][2:]

        self.assertEqual(rows(self.context), [
            "| 剩余 / 重置 | 83% / 1d2h | ==15==% / 4h |",
        ])
        document = dn.render(self.manifest, context=self.context)
        self.assertIn("| 额度 | Claude Code · Fable · 7d | Codex · prolite · 7d |", document)
        self.assertIn("额度快照 Claude Code 0.5h前；Codex 3h前", document.rsplit("\n---\n\n", 1)[1])
        self.context["quota"] = [
            {"name": "Low", "window": "5h", "left_percent": 30, "level": "low"},
            {"name": "Over", "window": "5h", "left_percent": 140, "level": "ok"},
            {"name": "Under", "window": "5h", "left_percent": -5, "level": "critical"},
            {"window": "a row without a name is skipped"},
        ]
        self.assertEqual(rows(self.context), [
            "| 剩余 / 重置 | **30**% | 100% | ==0==% |",
        ])

    def test_quota_windows_have_their_own_columns_and_escape_pipe_cells(self):
        self.context["quota"] = [
            {"name": "Claude | Code", "window": "5h", "left_percent": 100, "level": "ok",
             "reset_relative": "2h", "snapshot_age_hours": 0.5},
            {"name": "Codex", "window": "7d", "left_percent": 88, "level": "ok", "snapshot_age_hours": 0.5},
            {"name": "Claude | Code", "window": "7d", "left_percent": 0, "level": "critical",
             "reset_relative": "1d | delayed\nnext", "snapshot_age_hours": 0.5},
        ]
        document = dn.render(self.manifest, None, None, None, self.context)
        assert_native(document)
        block = next(block for block in document.split("\n\n") if block.startswith("| 额度 |"))
        headers, rows, rejected = rc._markdown_table("## Quota\n" + block, "Quota")
        self.assertEqual(rejected, [])
        self.assertEqual(headers, ["额度", "Claude ｜ Code · 5h", "Codex · 7d", "Claude ｜ Code · 7d"])
        self.assertEqual(len(rows), 1)
        self.assertIn("| 剩余 / 重置 | 100% / 2h | 88% | ==0==% / 1d ｜ delayed next |", block)
        self.assertNotIn("快照", block)
        self.assertEqual(document.count("额度快照 0.5h前"), 1)

    def test_context_is_optional_and_warnings_are_visible(self):
        plain = dn.render(self.manifest)
        self.assertNotIn("| 额度 |", plain)
        warned = dn.render(self.manifest, None, None, None, {"quota": [], "warnings": ["claude quota: no snapshot"]})
        self.assertIn("\n\n! claude quota: no snapshot\n\n", warned)

    def test_signal_counts_are_not_duplicated_in_the_masthead(self):
        """The action headings and due columns already carry these counts."""
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
        strip = self._strip(dn.render(self.manifest, overview, brief))
        self.assertEqual(strip, "")
        for noise in ("有产出", "待 review", "完成", "失败"):
            self.assertNotIn(noise, strip)

    def test_signal_strip_is_empty_when_nothing_is_live(self):
        plain = dn.render(self.manifest)
        zero = dn.render(self.manifest, {"schema": 1, "sections": []}, {"signals": {"closing": 0}})
        self.assertEqual((self._strip(plain), self._strip(zero)), ("", ""))

        failed_manifest = representative_digest_inputs()[0]
        failed_manifest["health"]["failed"] = 3
        failed = dn.render(failed_manifest)
        self.assertEqual(self._strip(failed), "")
        self.assertEqual(failed.count("历史失败 3 次"), 1)

        for age in (3, 4):
            with self.subTest(weight_age_days=age):
                brief = {"signals": {"weight_age_days": age}}
                document = dn.render(self.manifest, None, brief)
                self.assertEqual(self._strip(document), "")
                self.assertEqual(document.count(f"体重记录：{age}d 前"), 1)
        from daily_brief import Group, MERGED_HEADING_CHARS, apply_cap
        health = Group(tier=3, kind="health", heading="健康观测: 体重上次 2099-01-23 (7d 前)", folded=True)
        crowded = [Group(tier=3, kind="recurring", heading="recurring: 9 条逾期 (recurring.py list)", folded=True),
                   Group(tier=3, kind="review", heading="x" * MERGED_HEADING_CHARS, folded=True), health]
        folded, _, _ = apply_cap(crowded, cap=1)
        brief = {"signals": {"weight_age_days": 7}, "groups": [{"heading": g.display_heading()} for g in folded]}
        self.assertNotIn("体重", brief["groups"][0]["heading"])
        self.assertIn("recurring.py list", brief["groups"][0]["heading"])
        document = dn.render(self.manifest, None, brief)
        self.assertEqual(document.count("体重记录：7d 前"), 1)
        self.assertNotIn("recurring.py list", document)
        brief["groups"] = [{"heading": health.display_heading()}]
        document = dn.render(self.manifest, None, brief)
        self.assertEqual(document.count("体重"), 1)
        self.assertNotIn("体重记录：", document)

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
        document = dn.render(self.manifest, {"schema": 1}, brief)
        self.assertEqual(self._strip(document), "")
        for bit in ("routine 4/19 有产出", "2 完成", "149 待 review"):
            self.assertIn(bit, document)
            self.assertGreater(document.index(bit), document.index("## 来源索引"))
        self.assertEqual(document.count("recurring: 9 条逾期, 4 条刚到期"), 1)

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
        document = dn.render(self.manifest, None, brief)
        self.assertIn("## 需要开始处理 1 件\n\n- **42d** · Hotel credit · `x:1`\n\n"
                      "## TODO 到期 3 件\n\n- ==今天== · Notarize form\n- 4d · Review status\n"
                      "- ==逾期 2d== · Late thing", document)

    def test_tomorrow_acts_now_and_an_empty_ledger_says_so(self):
        brief = {"schema": 1, "date": "2099-01-30", "warnings": [], "groups": [
            {"tier": 2, "kind": "todo", "heading": "TODO 到期 1 件", "items": [{"text": "tomorrow thing", "days_left": 1}]}]}
        document = dn.render(self.manifest, None, brief)
        self.assertIn("## TODO 到期 1 件\n\n- ==明天== · tomorrow thing\n\n", document)
        empty = dn.render(self.manifest, None, {**brief, "groups": []})
        self.assertIn("## 今日\n\n今天没有关窗项、到期 TODO 或 review 债。\n\n", empty)

    def test_signal_strip_marks_a_lead_closing_and_an_unknown_fleet(self):
        self.manifest["health"]["state_unavailable"] = True
        brief = {"signals": {"closing": 1, "closing_now": 0}}
        document = dn.render(self.manifest, None, brief)
        self.assertEqual(self._strip(document), "")
        self.assertIn("Prefect 状态不可用", document)
        # Without fleet health there is no strip, however live the brief's cells are.
        self.manifest["health"] = {}
        self.assertEqual(self._strip(dn.render(self.manifest, None, {"signals": {"focus_days": 3}})), "")

    def test_colophon_line_names_each_bookkeeping_bit(self):
        def colophon(document: str) -> str:
            return document.rsplit("\n---\n\n", 1)[1].splitlines()[0]

        self.manifest["health"]["state_unavailable"] = True
        review = {"tier": 3, "kind": "review", "heading": "review 债 5 项", "items": [{"text": "a"}, {"text": "b"}]}
        brief = {"schema": 1, "date": "2099-01-30", "warnings": [], "groups": [review]}
        fleet = "4 KB 源文本 · 1 条状态更新 · 生成于 06:22 · routine 4/19 有产出 · 2 完成 · Prefect 状态不可用 · 149 待 review"
        tag = dn.note_tag(self.manifest)
        self.assertEqual(colophon(dn.render(self.manifest, None, brief, None, {"weather": {"place": ""}})),
                         fleet + " · " + tag)
        brief["groups"] = [{**review, "items": []}]
        self.assertEqual(colophon(dn.render(self.manifest, None, brief)), fleet + " · " + tag)
        self.manifest["health"] = {}
        self.assertEqual(colophon(dn.render(self.manifest)), "4 KB 源文本 · 1 条状态更新 · 生成于 06:22 · " + tag)

    def test_each_note_carries_one_tag_for_reflect(self):
        """Daily and backlog notes end with #日报, weekly roll-ups with #周报; field text never adds one."""
        live = re.compile(r"(?<!\S)#[\w-]*[^\W\d_][\w-]*")
        for mode in ("daily", "weekly"):
            manifest, overview, brief, picks, context = representative_digest_inputs(mode)
            overview = {**overview, "headline": "#日报 #other tag text"}
            note = dn.render(manifest, overview, brief, picks, context)
            expected = "#日报" if mode == "daily" else "#周报"
            with self.subTest(mode=mode):
                self.assertEqual(dn.note_tag(manifest), expected)
                self.assertEqual(live.findall(_CODE_SPAN_RE.sub("", note)), [expected])
                self.assertTrue(note.rsplit("\n---\n\n", 1)[1].split("\n", 1)[0].endswith(" · " + expected))
        backlog = {"mode": "daily", "selection": "unacked", "window": {"since": "2099-01-01", "until": "2099-01-30"}}
        self.assertEqual(dn.note_tag(backlog), "#日报")

    def test_weather_line_keeps_only_what_it_knows(self):
        context = {"weather": {"place": "P", "tmin": 1, "tmax": 2, "summary": "s", "precip_probability": None,
                               "hours": [{"hour": "x", "temp": 1}, {"hour": 9, "temp": 18}], "date": "2099-01-29"}}
        # No precipitation figure, no hour that is not a number, and the forecast's own date when it differs.
        self.assertIn("\n\n**P** 1–2°C · s · 9:00 18° · 2099-01-29\n\n", dn.render(self.manifest, None, None, None, context))
        placeless = dn.render(self.manifest, None, None, None, {"weather": {"place": "", "tmin": 1, "tmax": 2}})
        self.assertNotIn("°C", placeless)
        self.assertNotIn("天气 Open-Meteo", placeless)

    def test_masthead_order_runs_weather_strip_quota_then_warnings_before_the_ledger(self):
        manifest, _overview, brief, _picks, context = representative_digest_inputs()
        document = dn.render(manifest, None, brief, None, context)
        self.assertEqual(self._strip(document), "")
        offsets = [document.index(needle) for needle in (
            "\n**Lisbon** ", "\n## 模型额度", "\n| 额度 |", "\n! synthetic context warning\n",
            "\n## 需要开始处理 1 件")]
        self.assertEqual(offsets, sorted(offsets))


class FrontierAndCuratedDepthTests(unittest.TestCase):
    def setUp(self):
        self.manifest = representative_digest_inputs()[0]

    def _labs(self, sweep_date):
        labs = representative_digest_inputs()[1]["frontier_labs"]
        labs["sweep_date"] = sweep_date
        return labs

    def test_frontier_lists_signals_on_the_sweep_day_and_the_day_after(self):
        for sweep in ("2099-01-30", "2099-01-29"):
            with self.subTest(sweep=sweep):
                document = dn.render(self.manifest, {"schema": 1, "frontier_labs": self._labs(sweep)})
                self.assertIn("## 前沿实验室 · 1 条信号 · 0 漂移 · 1 晋级 · 扫描 " + sweep[5:], document)
                self.assertIn("**Example Lab**\n\n- 模型发布 · 1级来源 · Atlas early access shipped. · "
                              "[来源 ↗](https://example.com/atlas)", document)
                self.assertIn("\n\nNo mission drift in the synthetic fixture.\n\n", document)
                self.assertLess(document.index("前沿实验室"), document.index("以上 "))

    def test_frontier_collapses_to_counts_on_later_days(self):
        document = dn.render(self.manifest, {"schema": 1, "frontier_labs": self._labs("2099-01-25")})
        self.assertIn("1 条信号 · 0 漂移 · 1 晋级 · 扫描 01-25", document)
        self.assertNotIn("Example Lab", document)
        self.assertIn("本期扫描 2099-01-25 已随当日 digest 报告，之后尚无新扫描。", document)

    def test_frontier_is_absent_without_the_field(self):
        self.assertNotIn("前沿实验室", dn.render(self.manifest, {"schema": 1}))

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
        document = dn.render(self.manifest, overview)
        self.assertIn("## 信号精选 · 1 / 5 · 1 分钟", document)
        self.assertIn("- **HBM 层数增加压缩良率** · [来源 ↗](https://example.com/hbm)\n"
                      "  - 第一点。\n  - 第二点。\n  - 为何重要：支撑 **MU** 的定价。", document)
        self.assertNotIn("第三点不该出现。", document)
        # The depth layer carries no routine body; the source index below it
        # navigates by unit link, not by excerpt.
        depth = document[document.index("以上 "):document.index("## 来源索引")]
        self.assertNotIn("The rate corridor was held", depth)
        # The feed's own items follow, deterministically, from the manifest.
        self.assertIn("## 科技动态 · 1 · 1 分钟\n\n- [Example Model](https://example.com/model)\n"
                      "  - 示例模型发布了可复核的更新。", document)
        self.assertIn("## 信号精选 · 1 / 5", document)
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
        document = dn.render(self.manifest, self._finance_only_pick())
        self.assertIn("\n\n! 情报精选没有 Research 条目", document)
        self.assertGreater(document.index("! 情报精选"), document.index("## 信号精选"))

    def test_research_entry_or_research_free_window_is_silent(self):
        self.assertIsNone(core.deep_read_lane_gap(self._finance_only_pick("Research")["deep_read"], self.manifest))
        research_free = representative_digest_inputs()[0]
        research_free["lanes"] = [
            lane for lane in research_free["lanes"] if lane.get("lane") != "Research"
        ]
        self.assertIsNone(core.deep_read_lane_gap(self._finance_only_pick()["deep_read"], research_free))
        self.assertIsNone(core.deep_read_lane_gap({"total": 0, "entries": []}, self.manifest))
        self.assertNotIn("! 情报精选", dn.render(self.manifest, self._finance_only_pick("Research")))

    def test_without_curation_the_feed_and_index_render_never_bodies(self):
        for overview in (None, {"schema": 1}):
            with self.subTest(curated=overview is not None):
                document = dn.render(self.manifest, overview)
                self.assertNotIn("信号精选", document)
                self.assertIn("## 科技动态 · 1 · ", document)
                self.assertIn("## 科技动态 · 1 · 1 分钟\n\n- [Example Model](https://example.com/model)", document)
                depth = document[document.index("以上 "):document.index("## 来源索引")]
                self.assertNotIn("A compact systems result with a bounded source trail.", depth)
                self.assertIn("`research/example/2099-01-30-note.md`", document)
                self.assertNotIn("\n  - [example result](https://example.com/research)\n", document)

    def _frontier(self, labs: dict) -> str:
        document = dn.render(self.manifest, {"schema": 1, "frontier_labs": labs})
        return "## 前沿实验室" + document.split("## 前沿实验室", 1)[1].split("\n\n## ", 1)[0].split("\n\n---\n\n", 1)[0]

    def test_frontier_groups_labs_in_first_seen_order_and_needs_lab_and_text(self):
        signals = [{"lab": "B", "text": "b"}, {"lab": "A", "text": "a"}, {"lab": "", "text": "x"}, {"lab": "C"}]
        self.assertEqual(self._frontier({"sweep_date": "2099-01-30", "signals": signals}),
                         "## 前沿实验室 · 2 条信号 · 0 漂移 · 0 晋级 · 扫描 01-30\n\n**B**\n\n- b\n\n**A**\n\n- a")
        self.assertEqual(self._frontier({"sweep_date": "2099-01-30", "signals": [
            {"lab": "Example Lab", "text": ""}, {"lab": "Other Lab"}]}),
            "## 前沿实验室 · 0 条信号 · 0 漂移 · 0 晋级 · 扫描 01-30")
        # A sweep with no date is listed in full.
        self.assertEqual(self._frontier({"sweep_date": "", "signals": [{"lab": "L", "text": "t"}]}),
                         "## 前沿实验室 · 1 条信号 · 0 漂移 · 0 晋级\n\n**L**\n\n- t")

    def test_frontier_collapses_from_two_days_after_the_sweep(self):
        for labs in ({"sweep_date": "2099-01-28", "signals": [{"lab": "L", "text": "t"}], "watchlist_note": "note"},
                     {"sweep_date": "2099-01-25", "signals": [], "watchlist_note": "note"}):
            with self.subTest(labs=labs):
                self.assertTrue(self._frontier(labs).endswith(
                    f"\n\n本期扫描 {labs['sweep_date']} 已随当日 digest 报告，之后尚无新扫描。"))
        # Nothing to report is the heading alone, never the collapsed sentence.
        self.assertEqual(self._frontier({"sweep_date": "2099-01-01", "signals": [], "watchlist_note": ""}),
                         "## 前沿实验室 · 0 条信号 · 0 漂移 · 0 晋级 · 扫描 01-01")

    def test_routine_summaries_cap_at_the_default_and_carry_the_manifest_label(self):
        overview = {"schema": 1, "routines": [
            {"path": "x/unknown.md", "summary": "\n".join(f"第 {n} 行" for n in range(1, 11))},
            {"path": "research/example/2099-01-30-note.md", "summary": "一行"}]}
        document = dn.render(self.manifest, overview)
        lines = [f"  - 第 {n} 行" for n in range(1, core.DEFAULT_ROUTINE_LINES + 1)]
        self.assertIn("## routine 摘要\n\n- **unknown.md** · 已截至 8 行 · `x/unknown.md`\n" + "\n".join(lines) +
                      "\n- **example research note** · `research/example/2099-01-30-note.md`\n  - 一行\n\n", document)

    def test_curated_picks_need_a_title_and_substance_and_keep_two_real_facts(self):
        overview = {"schema": 1, "deep_read": {"entries": [
            {"title": "", "facts": ["untitled fact"]}, {"title": "Bare title", "lane": "Research"},
            {"title": "u", "lane": "Research", "facts": ["", " ", "a", "b", "c"]}]}}
        document = dn.render(self.manifest, overview)
        self.assertIn("## 信号精选 · 1 · 1 分钟\n\n- **u**\n  - a\n  - b\n\n", document)
        for dropped in ("untitled fact", "Bare title"):
            self.assertNotIn(dropped, document)

    def test_the_feed_lists_titled_items_and_an_empty_depth_is_omitted(self):
        self.manifest["lanes"][0]["sources"][0]["items"] = [
            {"title": "", "note": "无标题条目。"}, {"title": "T", "url": "https://example.com/t", "note": "中文。"}]
        document = dn.render(self.manifest)
        self.assertIn("## 科技动态 · 1 · 1 分钟\n\n- [T](https://example.com/t)\n  - 中文。\n\n",
                      document)
        self.assertNotIn("无标题条目", document)
        quiet = {**self.manifest, "lanes": [lane for lane in self.manifest["lanes"] if lane["lane"] != "Tech feed"]}
        self.assertNotIn("情报详读", dn.render(quiet))

    def test_the_lane_gap_sits_directly_under_the_depth_heading(self):
        document = dn.render(self.manifest, self._finance_only_pick())
        below = document.split("\n## 信号精选 · ", 1)[1].split("\n\n", 2)[1]
        self.assertTrue(below.startswith("! 情报精选没有 Research 条目"), below)

    def test_recall_needs_an_excerpt_and_counts_days_under_a_year(self):
        picks = [{"reviewed": True, "title": "blank", "excerpt": "  "},
                 {"reviewed": True, "title": "t", "age_days": 90, "tier": "wiki", "excerpt": "e", "path": "wiki/x.md"}]
        document = dn.render(self.manifest, None, None, picks)
        self.assertIn("## 随机回顾 · 1 分钟\n\n- **t** · 90 天前 · wiki · `wiki/x.md`\n  - e\n\n", document)
        self.assertNotIn("**blank**", document)

    def test_source_index_details(self):
        excerpt = "abcdefghij " * 20
        manifest = {
            "schema": 1, "mode": "daily", "window": {"since": "2099-01-30", "until": "2099-01-30"},
            "generated": "2099-01-30T06:20:00", "counts": {"files": 4, "bytes": 1024}, "truncated": True,
            "lanes": [
                {"lane": "Research", "files": 3, "sources": [
                    {"path": "research/a/2099-01-30-list.md", "label": "list report", "date": "2099-01-30",
                     "items": [{"title": f"Item {n}", "url": f"https://example.com/{n}"} for n in range(1, 8)]},
                    {"path": "research/b/2099-01-30-links.md", "label": "link report", "date": "2099-01-30",
                     "primary_urls": [f"https://example.com/p{n}" for n in range(1, 4)]},
                    {"path": "research/c/undated.md", "label": "long report", "date": "2099-01-30",
                     "date_source": "mtime", "anchor": "src-shared", "excerpt": excerpt}]},
                {"lane": "Tech feed", "files": 1, "sources": [
                    {"path": "inbox/feed/2099-01-30-feed.md", "label": "feed", "date": "2099-01-30",
                     "items": [{"title": "One", "url": "https://example.com/one", "note": "中文一。"},
                               {"title": "Two", "url": "https://example.com/two", "note": "中文二。"}]}]}],
            "context_sources": {
                "shared": {"path": "research/sweeps/2099-01-29.md", "label": "sweep", "date": "2099-01-29",
                           "anchor": "src-shared"},
                "own": {"path": "research/sweeps/2099-01-28.md", "label": "older sweep", "date": "2099-01-28",
                        "anchor": "src-own"}},
        }
        index = dn.render(manifest).split("## 来源索引\n\n", 1)[1].split("\n\n---\n\n", 1)[0]
        self.assertEqual(index.split("\n\n"), [
            "**Research · 3**",
            "- **list report** · `research/a/2099-01-30-list.md`\n"
            "- **link report** · `research/b/2099-01-30-links.md`\n"
            "- **long report** · `research/c/undated.md` · (date from mtime)",
            "**Tech feed · 1**",
            "- **feed** · `inbox/feed/2099-01-30-feed.md`",
            # The sweep that shares an anchor with a fresh source is already listed above.
            "**背景来源**",
            "- **older sweep** · `research/sweeps/2099-01-28.md` · 2099-01-28",
            "Selection was truncated by --max-files; narrow the window.",
            dn.NO_OVERVIEW,
        ])
        empty = dn.render({**manifest, "lanes": [], "context_sources": {}, "truncated": False})
        self.assertIn("## 来源索引\n\nNo routine output in this window.\n\n", empty)

    def test_input_gaps_keep_their_order_and_name_every_failed_status(self):
        manifest = {
            "schema": 1, "mode": "daily", "window": {"since": "2099-01-30", "until": "2099-01-30"},
            "generated": "2099-01-30T06:20:00", "counts": {"files": 3, "bytes": 1024}, "context_warnings": ["w"],
            "lanes": [
                {"lane": "Tech feed", "files": 1, "sources": [
                    {"path": "inbox/feed/2099-01-30-feed.md", "label": "feed", "date": "2099-01-30",
                     "items": [{"title": "A", "note": "中文"}, {"note": "x"}]}]},
                {"lane": "Research", "files": 2, "sources": [
                    {"path": "research/a.md", "label": "partial run", "date": "2099-01-30", "meta": {"status": "partial"}},
                    {"path": "research/b.md", "label": "failed run", "date": "2099-01-30",
                     "meta": {"status": "error", "channels_reached": "0/3"}, "excerpt": "no channel answered"}]}],
        }
        overview = {"schema": 1, "gaps": ["g"], "sections": [{"title": dn.DECISION_SECTION, "bullets": [{"text": "q"}]}]}
        document = dn.render(manifest, overview)
        gaps = document.split("**输入缺口**\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(gaps.splitlines(), [
            "- g",
            f"- {dn.DECISION_SECTION} #1 缺 between/settles，已降级为{dn.SIGNAL_SECTION}",
            "- w",
            # The untitled item still owes a Chinese note.
            "- 科技动态 2 条中 2 条有摘要，1 条为中文；资讯例程需按「链接下一行缩进、中文、一句话」写摘要",
        ])
        self.assertIn("- **partial run** · `research/a.md` · 部分覆盖", document)
        self.assertIn("- **failed run** · `research/b.md` · 错误 · 0/3", document)
        self.assertNotIn("no channel answered", document)


class ReviewFollowUpTests(unittest.TestCase):
    """Cross-file contracts and the fail-closed paths the system review asked for."""

    def test_schema_constants_agree_across_modules(self):
        import daily_brief
        import daily_context

        self.assertEqual(rc.BRIEF_SCHEMA, daily_brief.BRIEF_SCHEMA)
        self.assertEqual(rc.CONTEXT_SCHEMA, daily_context.CONTEXT_SCHEMA)

    def test_malformed_overview_section_is_skipped_not_fatal(self):
        manifest = {"schema": core.MANIFEST_SCHEMA, "mode": "daily", "window": {"since": "2099-01-30", "until": "2099-01-30"}, "counts": {"files": 0, "bytes": 0}, "lanes": []}
        document = dn.render(manifest, {"schema": 1, "sections": ["not an object", {"title": "信号", "bullets": [{"text": "ok"}]}]})
        gaps = document.split("**输入缺口**\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(gaps, "- overview section #1 malformed; skipped")
        self.assertIn("## 信号 · 1\n\n- ok", document)

    def test_frontier_fails_closed_without_a_digest_date(self):
        labs = {"sweep_date": "2099-01-30", "signals": [{"lab": "Example Lab", "text": "Atlas."}]}
        manifest = representative_digest_inputs()[0]
        undated = representative_digest_inputs()[0]
        undated["window"]["until"] = ""
        overview = {"schema": 1, "frontier_labs": labs}
        self.assertNotIn("Example Lab", dn.render(undated, overview))
        self.assertIn("Example Lab", dn.render(manifest, overview))

    def test_recurring_counts_are_shown_once_using_daily_brief_wording(self):
        """The reminder preserves the collector's counts without a second footer count."""
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
        document = dn.render(representative_digest_inputs()[0], brief=brief)
        self.assertEqual(document.count(groups[0].display_heading().split(" (", 1)[0]), 1)
        self.assertIn("2 条逾期", document)
        self.assertNotIn("recurring 逾期", document)


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
        return dn.render(self.manifest, None, self._brief(item))

    @staticmethod
    def _ledger(document: str) -> list[str]:
        """The first action group's item and hint lines."""
        today = document.split("\n## ", 1)[1].split("\n\n## ", 1)[0]
        return [line for line in today.splitlines() if line.startswith(("- ", "  - "))]

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
        # The trace chip keeps enough of the stem to recognise the file.
        self.assertEqual(self._ledger(document), [
            "- **42d** · Hotel credit · `2099-01-30-decision-e…:123`",
            "  - book one night",
        ])
        self.assertEqual(document.count("42d"), 1)
        self.assertNotIn("Hotel credit · 42d", document)
        self.assertNotIn("long-note-name.md", document)
        assert_native(document)

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
        self.assertEqual(self._ledger(document), [
            "- **39d** · Example Hotel 免房券 · `example-tracker:51` · ==待核== `Example City 2099-01-…:13`",
        ])

    def _decision(self, **extra):
        return {
            "schema": 1,
            "sections": [
                {
                    "title": dn.DECISION_SECTION,
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
        document = dn.render(self.manifest, overview)
        card = document.split(f"## {dn.DECISION_SECTION} · 1\n\n", 1)[1].split("\n\n", 1)[0]
        # The fixture's feed file is dated 01-29, so the cited 01-30 path is marked unmatched.
        self.assertEqual(card.splitlines(), [
            "- 是否把验证预算给 Example Model 2",
            "  - A. 现在更新跟踪结论",
            "  - B. 继续保留为候选",
            "  - 定案：完整模型卡与独立基准 · 截止 2099-02-01 · `2099-01-30-feed.md` (unmatched)",
        ])
        self.assertEqual(len(dn.normalize_decisions(overview)[0]["sections"][0]["bullets"]), 1)
        self.assertNotIn("已降级", document)
        assert_native(document)

    def test_unstructured_decision_is_demoted_to_signals_and_named_in_the_colophon(self):
        overview = self._decision()
        normalized, notes = dn.normalize_decisions(overview)
        self.assertEqual(notes, [f"{dn.DECISION_SECTION} #1 缺 between/settles，已降级为{dn.SIGNAL_SECTION}"])
        self.assertEqual(normalized["sections"][0]["bullets"], [])
        titles = [s["title"] for s in normalized["sections"]]
        self.assertEqual(titles, [dn.DECISION_SECTION, dn.SIGNAL_SECTION])
        self.assertEqual(normalized["sections"][1]["bullets"][0]["text"], "是否把验证预算给 Example Model 2")
        # The caller's overview is untouched; the note names the demotion under 输入缺口.
        self.assertEqual(len(overview["sections"]), 1)
        document = dn.render(self.manifest, overview)
        self.assertIn(f"## {dn.DECISION_SECTION} · 0\n\n## {dn.SIGNAL_SECTION} · 1", document)
        self.assertNotIn("  - 定案：", document)
        self.assertIn(f"\n- {notes[0]}\n", document.split("**输入缺口**", 1)[1])
        self.assertLess(document.index("是否把验证预算"), document.index("以上 "))

    def test_demotion_into_an_existing_signal_section_leaves_the_caller_untouched(self):
        overview = self._decision()
        overview["sections"].append({"title": dn.SIGNAL_SECTION, "bullets": [{"text": "已有信号"}]})
        first, _ = dn.normalize_decisions(overview)
        second, _ = dn.normalize_decisions(overview)
        self.assertEqual([b["text"] for b in overview["sections"][1]["bullets"]], ["已有信号"])
        for result in (first, second):
            self.assertEqual(
                [b["text"] for b in result["sections"][1]["bullets"]],
                ["已有信号", "是否把验证预算给 Example Model 2"],
            )
        dn.render(self.manifest, overview)
        self.assertEqual(dn.render(self.manifest, overview).count("是否把验证预算给"), 1)

    def test_two_decision_sections_keep_their_own_cards(self):
        valid = {"text": "决策甲", "between": ["a", "b"], "settles": "s"}
        second = {"text": "决策乙", "between": ["c", "d"], "settles": "t"}
        overview = {
            "schema": 1,
            "sections": [
                {"title": dn.DECISION_SECTION, "bullets": [valid, {"text": "伪一"}]},
                {"title": dn.DECISION_SECTION, "bullets": [second, {"text": "伪二"}]},
            ],
        }
        normalized, notes = dn.normalize_decisions(overview)
        decisions = [s for s in normalized["sections"] if s["title"] == dn.DECISION_SECTION]
        self.assertEqual([[b["text"] for b in s["bullets"]] for s in decisions], [["决策甲"], ["决策乙"]])
        signals = [s for s in normalized["sections"] if s["title"] == dn.SIGNAL_SECTION]
        self.assertEqual([b["text"] for b in signals[0]["bullets"]], ["伪一", "伪二"])
        # Numbering runs across sections, so two demotions never both say #1.
        self.assertEqual(notes, [
            f"{dn.DECISION_SECTION} #2 缺 between/settles，已降级为{dn.SIGNAL_SECTION}",
            f"{dn.DECISION_SECTION} #4 缺 between/settles，已降级为{dn.SIGNAL_SECTION}",
        ])
        document = dn.render(self.manifest, overview)
        self.assertEqual(document.count("决策甲"), 1)
        self.assertEqual(document.count("决策乙"), 1)
        # The masthead counts what the page shows: two cards, two sections.
        self.assertEqual(len(decisions), 2)
        self.assertEqual(document.count("## 需要的决策 · 1"), 2)

    def test_a_decision_with_one_option_is_not_a_decision(self):
        self.assertEqual(dn.decision_shape_missing({"text": "q", "between": ["only"], "settles": "x"}), ["between"])
        self.assertEqual(dn.decision_shape_missing({"text": "q", "between": ["a", "b"]}), ["settles"])
        self.assertEqual(dn.decision_shape_missing("prose"), ["text", "between", "settles"])

    def test_options_that_open_with_inline_markup_still_count(self):
        overview = self._decision(
            between=["**现在更新**", "`候选` 保留", "[看](https://example.com/x)"],
            settles="**证据**",
        )
        normalized, notes = dn.normalize_decisions(overview)
        self.assertEqual((len(normalized["sections"][0]["bullets"]), notes), (1, []))
        document = dn.render(self.manifest, overview)
        for line in ("\n  - A. **现在更新**\n", "\n  - B. `候选` 保留\n", "\n  - C. [看](https://example.com/x)\n",
                     "\n  - 定案：**证据** · "):
            self.assertIn(line, document)

    def test_a_decision_without_a_question_is_demoted_not_rendered_blank(self):
        for bullet in ({"between": ["a", "b"], "settles": "s"}, {"text": "  ", "between": ["a", "b"], "settles": "s"}):
            with self.subTest(bullet=bullet):
                self.assertEqual(dn.decision_shape_missing(bullet), ["text"])
                overview = {"schema": 1, "sections": [{"title": dn.DECISION_SECTION, "bullets": [bullet]}]}
                normalized, notes = dn.normalize_decisions(overview)
                self.assertEqual(normalized["sections"][0]["bullets"], [])
                self.assertEqual(notes, [f"{dn.DECISION_SECTION} #1 缺 text，已降级为{dn.SIGNAL_SECTION}"])
                document = dn.render(self.manifest, overview)
                self.assertNotIn("  - A. ", document)
                self.assertEqual([line for line in document.splitlines() if line.strip() == "-"], [])

    def test_a_card_part_that_renders_to_nothing_is_missing(self):
        """Shape is judged on the rendered text: an image is dropped, so it is no option, settlement, or question."""
        image = "![chart](https://example.com/chart.png)"
        overview = {"schema": 1, "sections": [{"title": dn.DECISION_SECTION, "bullets": [
            {"text": "q", "between": [image, "B"], "settles": "s"},
            {"text": "q2", "between": ["A", "B"], "settles": image},
            {"text": image, "between": ["A", "B"], "settles": "s"}]}]}
        normalized, notes = dn.normalize_decisions(overview)
        self.assertEqual(notes, [f"{dn.DECISION_SECTION} #{n} 缺 {part}，已降级为{dn.SIGNAL_SECTION}"
                                 for n, part in ((1, "between"), (2, "settles"), (3, "text"))])
        self.assertEqual(normalized["sections"][0]["bullets"], [])
        document = dn.render(self.manifest, overview)
        self.assertNotIn("  - A. ", document)
        self.assertNotIn("定案：", document)

    def test_demotions_join_the_first_signal_section(self):
        overview = self._decision()
        overview["sections"] += [{"title": dn.SIGNAL_SECTION, "bullets": [{"text": "甲"}]},
                                 {"title": dn.SIGNAL_SECTION, "bullets": [{"text": "乙"}]}]
        normalized, _ = dn.normalize_decisions(overview)
        self.assertEqual([[bullet["text"] for bullet in section["bullets"]] for section in normalized["sections"]],
                         [[], ["甲", "是否把验证预算给 Example Model 2"], ["乙"]])

    def test_an_eighth_option_stays_a_two_level_item(self):
        overview = self._decision(between=[f"选项 {n}" for n in range(1, 9)], settles="s")
        document = dn.render(self.manifest, overview)
        self.assertIn("  - G. 选项 7\n", document)
        assert_native(document)

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
        document = dn.render(self.manifest, {"schema": 1, "frontier_labs": labs})
        self.assertEqual(document.count("**Example Lab / Long Name (org)**"), 1)
        self.assertIn("**Example Lab / Long Name (org)**\n\n"
                      "- 模型发布 · 1级来源 · Ling 3.0 出现。 · [来源 ↗](https://example.com/a)\n"
                      "- 图像模型 · 1级来源 · LLaDA-Image 上线。 · [来源 ↗](https://example.com/b)", document)
        self.assertIn("扫描 01-30", document)
        self.assertNotIn("周扫", document)

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
        self.assertIsNone(dn.feed_note_gap(self.manifest))
        items = self.manifest["lanes"][0]["sources"][0]["items"]
        items[:] = [
            {"title": "A", "url": "https://example.com/a", "note": "English only."},
            {"title": "B", "url": "https://example.com/b"},
            {"title": "C", "url": "https://example.com/c"},
            {"title": "D", "url": "https://example.com/d"},
        ]
        gap = dn.feed_note_gap(self.manifest)
        self.assertIn("科技动态 4 条中 1 条有摘要，0 条为中文", gap)
        document = dn.render(self.manifest, {"schema": 1})
        self.assertIn("科技动态 4 条中 1 条有摘要", document)
        self.assertGreater(document.index("科技动态 4 条中"), document.index("以上 "))
        # Any item without a Chinese note is reported; the spec promises one
        # per item, and a tolerance would hide the partial failure.
        for item, note in zip(items, ("中文一。", "English two.", "English three.", "English four.")):
            item["note"] = note
        self.assertIn("4 条中 4 条有摘要，1 条为中文", dn.feed_note_gap(self.manifest))
        items[1]["note"] = "中文二。"
        self.assertIn("4 条中 4 条有摘要，2 条为中文", dn.feed_note_gap(self.manifest))
        items[2]["note"] = "中文三。"
        items[3]["note"] = "中文四。"
        self.assertIsNone(dn.feed_note_gap(self.manifest))
        del items[3]["note"]
        self.assertIn("4 条中 3 条有摘要，3 条为中文", dn.feed_note_gap(self.manifest))

    def test_a_feed_of_bare_headlines_is_named_in_the_gaps(self):
        for item in self.manifest["lanes"][0]["sources"][0]["items"]:
            item.pop("note")
        overview = {
            "schema": 1,
            "deep_read": {
                "total": 1,
                "entries": [{"title": "curated", "facts": ["fact"], "why": "why"}],
            },
        }
        document = dn.render(self.manifest, overview)
        feed = document.split("## 科技动态 · 2 · 1 分钟\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(feed.splitlines(), ["- [One](https://example.com/one)", "- [Two](https://example.com/two)"])
        gaps = document.split("**输入缺口**\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("2 条中 0 条有摘要", gaps)


# Every string reaches every untrusted field at once (design probes plus three
# reviews'); together they found three neutralizer gaps, so the list stays whole.
HOSTILE = [
    "+ [ ] not a task", "- [ ] box", "- [x] done", "[x] done", "[ ] open", "[~] cancel", "[/] partial",
    "1. numbered", "1) numbered", "> quote", "> [!NOTE] callout", "[!WARNING] callout", "# H1", "## H2",
    "#tag", "#AI tooling", "C# and #2", "中文 #标签 尾", "![img](https://example.com/x.png)", "![[Embed]]",
    "[[Wiki]]", "[[Wiki|alias]]", "[[Owner#Row|sot]]", "[[2099-01-29]]", "[js](javascript:alert(1))",
    "[rel](notes/x.md)", "[angle](<a b.md>)", "[head](#top)", "[data](data:text/html,x)", "[proto](//evil.example)",
    "<!-- hidden -->", "<!--", "-->", "<b>bold</b>", "<script>alert(1)</script>", "<img src=x onerror=y>",
    "<?php echo 1 ?>", "<https://example.com/auto>", "&amp; &lt; &#39; &hellip; &mdash;", "[^1] footnote",
    "text ^blockid", "key:: value", "%%comment%%", "%%", "line one\nline two", "第一行\n第二行", "x\n# injected",
    "x\n- injected", "---", "***", "___", "- - -",
    "* * *", "===", "```python", "```", "~~~", "~~~js", "$$", "$x$ and $y$", "[ref]: https://example.com",
    "[ref]: <x>", "`unpaired", "`code` ok", "``double``", "trailing backslash \\", "\\# escaped", "| a | b |",
    "|---|---|", "**bold** _em_ ~~strike~~ ==hl==", "a**b", "[[unclosed", "]]", "[Show HN] title",
    "Mercury (planet)", "[nested [brackets]](https://example.com)", "https://bare.example.com/path",
    "www.example.com", "mail@example.com", "  leading spaces", "\ttab", "    four-space indent", "🚀 launch",
    "-1.5% move", "*", "-", "+", "1.", "#", ">", "", "   ",
]
# The notes the hostile render cites, by path and H1; every other [[X]] would be foreign.
HOSTILE_VAULT = {
    "research/example/2099-01-30-note.md": "A compact systems result",
    "inbox/feed/2099-01-29-feed.md": "Example Feed Digest: 2099-01-29",
    "personal/example-status.md": "Example Status Ledger",
    "wiki/example.md": "An Earlier Example",
    "research/example/sweeps/2099-01-29-sweep.md": "Example Lab Sweep: 2099-01-29",
}


def _hostile_note(hostile: str, titles: TitleIndex) -> str:
    """The note with `hostile` in every untrusted field; it always has ten H2s."""
    item = {"text": hostile, "label": hostile, "hint": hostile, "days_left": 2, "source": f"gtd/{hostile}.md:4",
            "flag": hostile, "flag_source": f"notes/{hostile}.md:9"}
    brief = {"schema": 1, "date": "2099-01-30", "signals": {"closing": 1},
             "groups": [{"tier": 1, "kind": "closing_lead", "heading": hostile, "items": [item]},
                        {"tier": 2, "kind": "todo", "heading": hostile, "items": [{"text": hostile, "days_left": 0}]}],
             "warnings": [hostile]}
    source = {"path": "research/example/2099-01-30-note.md", "label": hostile, "date": "2099-01-30",
              "headline": hostile, "anchor": "a1", "excerpt": hostile, "meta": {"status": "degraded",
              "channels_reached": hostile}, "units": [{"slug": hostile, "source_url": hostile}]}
    feed = {"path": "inbox/feed/2099-01-29-feed.md", "label": hostile, "date": "2099-01-29", "headline": hostile,
            "items": [{"title": hostile, "url": hostile, "note": hostile},
                      {"title": f"x {hostile}", "url": "https://example.com/ok", "note": hostile}]}
    other = {"path": f"career/{hostile}.md", "label": hostile, "date": "2099-01-30", "headline": hostile,
             "items": [{"title": hostile, "url": hostile}], "primary_urls": [hostile]}
    manifest = {
        "schema": 1, "mode": "daily", "window": {"since": "2099-01-30", "until": "2099-01-30"},
        "generated": "2099-01-30T06:20:00", "counts": {"files": 3, "updates": 1, "bytes": 2048},
        "health": {"declared": 2, "reported": 1, "failed": 1},
        "updates": [{"source": "s", "id": "i", "label": hostile, "date": hostile, "path": "personal/example-status.md",
                     "values": {hostile or "k": hostile}}],
        "update_warnings": [hostile], "skipped_routines": [hostile], "context_warnings": [hostile],
        "lanes": [{"lane": "Tech feed", "files": 1, "sources": [feed]},
                  {"lane": hostile or "Lane", "files": 2, "sources": [source, other]}],
        "context_sources": {"k": {"path": "research/example/sweeps/2099-01-29-sweep.md", "label": hostile,
                                  "date": "2099-01-29"}},
    }
    overview = {
        "schema": 1, "headline": hostile, "gaps": [hostile],
        "sections": [
            {"title": "需要的决策", "bullets": [{"text": f"q {hostile}", "between": [f"A {hostile}", f"B {hostile}"],
                                              "settles": f"s {hostile}", "by": hostile, "url": hostile,
                                              "sources": ["research/example/2099-01-30-note.md", hostile]}]},
            {"title": hostile, "note": hostile, "bullets": [hostile, {"text": hostile, "sources": [hostile]}]},
        ],
        "frontier_labs": {"sweep_date": "2099-01-29", "drift_count": hostile, "promotion_count": 1,
                          "signals": [{"lab": hostile, "category": hostile, "tier": hostile, "text": f"t {hostile}",
                                       "url": hostile}], "watchlist_note": hostile},
        "routines": [{"path": "research/example/2099-01-30-note.md", "summary": f"{hostile}\nsecond {hostile}"}],
        "articles": [{"title": f"a {hostile}", "url": hostile, "minutes": hostile, "source": hostile, "why": hostile,
                      "abstract": f"abstract {hostile}"}],
        "deep_read": {"total": hostile, "entries": [{"title": f"d {hostile}", "url": hostile, "facts": [hostile, hostile],
                                                     "why": hostile, "lane": "Research"}]},
    }
    context = {"schema": 1, "date": "2099-01-30", "warnings": [hostile],
               "weather": {"place": f"p {hostile}", "tmin": hostile, "tmax": 3, "summary": hostile,
                           "precip_probability": hostile, "hours": [{"hour": 9, "temp": hostile}], "date": hostile},
               "quota": [{"name": f"n {hostile}", "window": hostile, "left_percent": hostile, "level": "low",
                          "reset_relative": hostile, "snapshot_age_hours": hostile}]}
    retro = [{"reviewed": True, "title": hostile, "excerpt": f"e {hostile}", "age_days": hostile, "tier": hostile,
              "path": "wiki/example.md"}]
    return dn.render(manifest, overview, brief, retro, context, titles=titles)


def _visible_words(hostile: str) -> list[str]:
    """Words a reader must still see: images drop, and links and wikilinks keep only their text."""
    text = html.unescape(hostile)
    text = re.sub(r"!\[[^\]\n]*\]\([^)\n]*\)", " ", text)
    text = re.sub(r"!?\[\[(?:[^\[\]\n|]*\|)?([^\[\]\n]*)\]\]", r" \1 ", text)
    text = re.sub(r"\]\([^)\n]*\)", "] ", text)
    return re.findall(r"[A-Za-z]{2,}|[一-鿿]+", text)


class NoteFormatTests(unittest.TestCase):
    """Every note is Reflect-native, and untrusted text is inert wherever it lands."""

    def test_fixture_notes_are_reflect_native(self):
        manifest, overview, brief, retrospect, context = representative_digest_inputs("daily")
        for name, document in (
            ("curated daily", dn.render(manifest, overview, brief, retrospect, context)),
            ("scheduled daily", dn.render(manifest, None, brief)),
            ("weekly", dn.render(*representative_digest_inputs("weekly"))),
        ):
            with self.subTest(note=name):
                assert_native(document)
                self.assertRegex(document, r"\A---\ncurated: (true|false)\n---\n\n# Atelier (Daily|Weekly): ")
                self.assertTrue(document.endswith("\n") and not document.endswith("\n\n"))
                self.assertNotIn("\r", document)

    def test_untrusted_text_is_inert_in_every_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            for rel, title in HOSTILE_VAULT.items():
                (vault / rel).parent.mkdir(parents=True, exist_ok=True)
                (vault / rel).write_text(f"# {title}\n", encoding="utf-8")
            titles = TitleIndex(vault)
            where = vault / "inbox/digest/2099-01/2099-01-30-daily-digest.md"
            for hostile in HOSTILE:
                with self.subTest(hostile=hostile):
                    document = _hostile_note(hostile, titles)
                    assert_native(document, titles)
                    self.assertEqual(nonnative(document, where, titles), Counter())
                    self.assertEqual(len(h2s(document)), 13)
                    bare = "\n".join(_CODE_SPAN_RE.sub("", line) for line in document.split("\n"))
                    self.assertEqual(re.findall(r"(?<!\S)#[\w-]*[^\W\d_][\w-]*", bare), ["#日报"],
                                     "only the note's own tag is live")
                    self.assertLessEqual(set(_WIKILINK.findall(bare)), set(HOSTILE_VAULT.values()))
                    self.assertEqual([url for url in re.findall(r"\]\(([^)\n]*)\)", bare)
                                      if not re.match(r"https?://", url)], [])
                    self.assertEqual([word for word in _visible_words(hostile) if word not in document], [])

    def test_a_two_dash_field_never_becomes_a_rule(self):
        """A gap, a 信号 bullet and a brief hint of '--' each render '- --' or '  - --', a thematic break
        in CommonMark and meowdown that ends the list."""
        manifest = representative_digest_inputs()[0]
        overview = {"schema": 1, "gaps": ["--"], "sections": [{"title": dn.SIGNAL_SECTION, "bullets": ["--"]}]}
        assert_native(dn.render(manifest, overview, {"schema": 1, "date": "2099-01-30", "groups": [
            {"tier": 2, "kind": "todo", "heading": "TODO", "items": [{"text": "x", "hint": "--"}]}]}))

    def test_citations_use_link_title_else_a_code_span(self):
        notes = {
            "research/unique.md": "# A Unique Result\n",
            "finance/a/policy.md": "# Policy Monitor\n",
            "finance/b/policy.md": "# Policy Monitor\n",
            "notes/2099-01-28.md": "No heading, so the bare date stem is the title.\n",
            "_tools/scout.md": "# Tool Scout\n",
        }
        manifest = {
            "schema": 1, "mode": "daily", "window": {"since": "2099-01-30", "until": "2099-01-30"},
            "generated": "2099-01-30T06:20:00", "counts": {"files": 2, "bytes": 1024},
            "lanes": [{"lane": "Research", "files": 2, "sources": [
                {"path": "research/unique.md", "label": "unique", "date": "2099-01-30", "headline": "A Unique Result"},
                {"path": "finance/a/policy.md", "label": "policy", "date": "2099-01-30", "headline": "Policy Monitor"},
            ]}],
        }
        overview = {"schema": 1, "sections": [{"title": dn.SIGNAL_SECTION, "bullets": [
            {"text": "cited", "sources": ["research/unique.md", "finance/a/policy.md", "finance/invented.md"]}]}]}
        brief = {"schema": 1, "date": "2099-01-30", "warnings": [], "groups": [
            {"tier": 2, "kind": "todo", "heading": "TODO 到期 1 件",
             "items": [{"text": "Follow up", "days_left": 2, "source": "research/unique.md:3"}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            for rel, text in notes.items():
                (vault / rel).parent.mkdir(parents=True, exist_ok=True)
                (vault / rel).write_text(text, encoding="utf-8")
            (vault / ".reflectignore").write_text("_tools/\n", encoding="utf-8")
            titles = TitleIndex(vault)
            self.assertEqual(dn.cite("research/unique.md", titles), "[[A Unique Result]]")
            # Ambiguous, a bare date, reflectignored, and missing: Reflect would open none of them by title.
            for path in ("finance/a/policy.md", "notes/2099-01-28.md", "_tools/scout.md", "research/missing.md"):
                with self.subTest(path=path):
                    self.assertEqual(dn.cite(path, titles), f"`{path}`")
            self.assertEqual(dn.cite("research/unique.md"), "`research/unique.md`")
            document = dn.render(manifest, overview, brief, titles=titles)
            assert_native(document, titles)
        self.assertIn("\n- cited · [[A Unique Result]] · `finance/a/policy.md` · `invented.md` (unmatched)\n", document)
        # A brief trace is navigation, never a backlink into the tracker.
        self.assertIn("\n- 2d · Follow up · `unique:3`\n", document)
        index = document.split("## 来源索引", 1)[1]
        # A headline equal to the link title is not printed twice.
        self.assertIn("\n- **unique** · [[A Unique Result]]\n", index)
        self.assertIn("\n- **policy** · `finance/a/policy.md`\n", index)

    def test_titles_reflect_would_read_as_paths_are_cited_by_path(self):
        # Path-shaped titles with an extension or a dot segment resolve to no note in Reflect.
        notes = {"research/lead.md": "/lead", "research/slashes.md": "a//b", "research/suffix.md": "x.md",
                 "research/version.md": "AI/ML weekly v2.1", "research/hidden.md": "a/.b"}
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            for rel, title in {**notes, "research/plan.md": "Q3/Q4 planning"}.items():
                (vault / rel).parent.mkdir(parents=True, exist_ok=True)
                (vault / rel).write_text(f"# {title}\n", encoding="utf-8")
            titles = TitleIndex(vault)
            for rel, title in notes.items():
                with self.subTest(title=title):
                    self.assertEqual(titles.link_title(Path(rel)), title)
                    self.assertEqual(dn.cite(rel, titles), f"`{rel}`")
            self.assertEqual(dn.cite("research/plan.md", titles), "[[Q3/Q4 planning]]")

    def test_app_links_hidden_tags_and_empty_items_stay_inert(self):
        """Only http(s) stays clickable; U+FEFF never shields a tag; nothing renders an empty item."""
        self.assertEqual(dn.md("see reflect://task?text=Pay and https://ok.example/a"),
                         "see reflect∶//task?text=Pay and https://ok.example/a")
        self.assertEqual(dn.plain("obsidian://open?vault=x"), "obsidian∶//open?vault=x")
        self.assertEqual(dn.md("[go](https://ok.example)"), "[go](https://ok.example)")
        self.assertEqual(dn.plain("a ﻿#tag"), "a ＃tag")
        labs = {"sweep_date": "2099-#tag", "drift_count": 0, "promotion_count": 0,
                "signals": [{"lab": "L", "text": "\x00"}, {"lab": "M", "text": "kept"}]}
        frontier = "\n".join(dn._frontier(labs, "2099-01-31"))
        self.assertNotRegex(frontier, r"(?<!\S)#\w")
        self.assertIn("1 条信号", frontier)
        self.assertNotRegex(frontier, r"(?m)^\s*-\s*$")
        manifest = {"updates": [{"label": "L", "date": "2099-01-31", "path": "x.md", "values": {"[ref]": "v"}}]}
        self.assertIn("\n  - ［ref]: v", "\n".join(dn._updates(manifest, None)))
        brief = {"groups": [{"heading": "h", "items": [{"hint": "only a hint"}]}]}
        self.assertNotRegex("\n".join(dn._brief(brief)), r"(?m)^\s*-\s*$")

    def test_frontmatter_records_whether_the_model_pass_ran(self):
        manifest, overview, brief, *_ = representative_digest_inputs("daily")
        scheduled, curated = dn.render(manifest, None, brief), dn.render(manifest, overview, brief)
        self.assertTrue(scheduled.startswith("---\ncurated: false\n---\n\n# Atelier Daily: 2099-01-30\n"))
        self.assertTrue(curated.startswith("---\ncurated: true\n---\n\n# Atelier Daily: 2099-01-30\n"))
        self.assertIn(dn.NO_OVERVIEW, scheduled)
        self.assertNotIn(dn.NO_OVERVIEW, curated)
        # An overview file holding only `{}` still means the model pass ran.
        self.assertTrue(dn.render(manifest, {}, brief).startswith("---\ncurated: true\n---\n"))

    def test_security_words_remain_safe_when_they_are_only_prose(self):
        manifest = representative_digest_inputs()[0]
        prose = (
            "Discuss javascript: links, file: paths, onerror= handlers, "
            "<svg/onload=alert(1)>, and <mj-include> as inert prose."
        )
        overview = {"schema": 1, "sections": [{"title": "Safety", "bullets": [{"text": prose}]}]}
        document = dn.render(manifest, overview)
        assert_native(document)
        self.assertIn("\n- Discuss javascript: links, file: paths, onerror= handlers, "
                      "＜svg/onload=alert(1)>, and ＜mj-include> as inert prose.\n", document)
        self.assertNotIn("<svg", document)
        self.assertNotIn("<mj-include", document)


class NoteCase(VaultCase):
    """Publishing helpers: the canonical note path, state bytes, and a quiet write."""

    def daily(self, until: str, **kwargs) -> dict:
        return rc.collect(self.vault, mode="daily", until=until, **kwargs)

    def note(self, manifest: dict) -> Path:
        return core.note_path(self.vault, manifest)

    def state(self) -> bytes | None:
        path = self.vault / rc.DIGEST_UPDATES_STATE
        return path.read_bytes() if path.exists() else None

    def write(self, text: str, manifest: dict, **kwargs) -> tuple[int, str]:
        """rd.write with its output captured: (exit code, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = rd.write(self.vault, text, manifest, **kwargs)
        return code, err.getvalue()


class LayoutTests(NoteCase):
    def test_note_names_and_month_buckets(self):
        """One note per day and kind under <paths.digest>/YYYY-MM/, whatever routine writes it."""
        cases = (
            (self.daily("2099-01-30"), "inbox/digest/2099-01/2099-01-30-daily-digest.md"),
            (self.manifest, "inbox/digest/2099-01/2099-01-30-weekly-digest.md"),
            (rc.collect(self.vault, mode="daily", until="2099-01-30", unacked=True),
             "inbox/digest/2099-01/2099-01-30-backlog-digest.md"),
        )
        for manifest, rel in cases:
            with self.subTest(note=rel):
                self.assertEqual(self.note(manifest).relative_to(self.vault).as_posix(), rel)
        for bad in ({"mode": "monthly", "window": {"until": "2099-01-30"}}, {"mode": "daily", "window": {"until": "x"}}):
            with self.subTest(manifest=bad), self.assertRaises(SystemExit):
                core.note_path(self.vault, bad)
        text = dn.render(self.manifest)
        for _ in range(2):
            self.assertEqual(self.write(text, self.manifest)[0], 0)
            self.assertEqual(self.note(self.manifest).read_text(encoding="utf-8"), text)


class SelfIngestionTests(NoteCase):
    def test_collect_never_reads_the_digest_notes(self):
        own = self.vault / "inbox/digest/2099-01"
        own.mkdir(parents=True)
        (own / "2099-01-30-daily-digest.md").write_text("# Atelier Daily: 2099-01-30\n", encoding="utf-8")
        registry = self.vault / "_tools/routines/registry.toml"
        registry.write_text(registry.read_text() + '''
[[routine]]
name = "careless-parent"
label = "careless parent"
output_dir = "inbox"
file_pattern = "**/*.md"
digest = { context = "parent_ctx" }
''', encoding="utf-8")
        for kwargs in ({}, {"include_maintenance": True}, {"unacked": True}):
            with self.subTest(kwargs=kwargs):
                manifest = rc.collect(self.vault, mode="weekly", until="2099-01-30", **kwargs)
                paths = [s["path"] for _, s in core.iter_sources(manifest)]
                self.assertFalse([p for p in paths if p.startswith("inbox/digest/")])
                self.assertIn("inbox/feed/2099-01-30-feed.md", paths)
                context = manifest["context_sources"].get("parent_ctx", {})
                self.assertFalse(str(context.get("path", "")).startswith("inbox/digest/"))
        # The careless row still sees its two feed files, never the note.
        self.assertEqual(core.hidden_by_ack(self.vault, "inbox", "", "zzz", set()), [("careless parent", 2)])

    def _digest_only_row(self, output_dir: str) -> None:
        """A note dated after every fixture file, and a row whose pattern matches only digest notes."""
        own = self.vault / "inbox/digest/2099-01"
        own.mkdir(parents=True)
        (own / "2099-01-31-daily-digest.md").write_text("# Atelier Daily: 2099-01-31\n", encoding="utf-8")
        registry = self.vault / "_tools/routines/registry.toml"
        registry.write_text(registry.read_text() + f'''
[[routine]]
name = "careless-parent"
label = "careless parent"
output_dir = "{output_dir}"
file_pattern = "**/*-digest.md"
digest = {{ context = "parent_ctx" }}
''', encoding="utf-8")

    def test_carry_context_and_health_never_count_a_digest_note(self):
        self._digest_only_row("inbox")
        carried = self.daily("2099-02-01")
        self.assertEqual([s["path"] for _, s in core.iter_sources(carried) if s["path"].startswith("inbox/digest/")], [])
        weekly = rc.collect(self.vault, mode="weekly", until="2099-01-31")
        self.assertNotIn("parent_ctx", weekly["context_sources"])
        careless = [routine for routine in core.load_routines(self.vault) if routine.name == "careless-parent"]
        health = rc.collect_health(self.vault, careless, {}, date(2099, 1, 25), date(2099, 1, 31))
        self.assertEqual((health["reported"], health["review_debt"]), (0, 0))

    def test_a_differently_cased_row_still_skips_the_digest_notes(self):
        self._digest_only_row("Inbox")
        if not (self.vault / "INBOX").is_dir():
            self.skipTest("the filesystem is case-sensitive, so 'Inbox' matches nothing")
        manifest = rc.collect(self.vault, mode="weekly", until="2099-01-31")
        paths = [s["path"] for _, s in core.iter_sources(manifest)]
        self.assertEqual([path for path in paths if path.casefold().startswith("inbox/digest/")], [])

    def test_a_row_through_a_symlinked_folder_still_skips_the_digest_notes(self):
        (self.vault / "alias").symlink_to(self.vault / "inbox", target_is_directory=True)
        self._digest_only_row("alias")
        manifest = rc.collect(self.vault, mode="weekly", until="2099-01-31")
        own = (self.vault / "inbox/digest").resolve()
        paths = [s["path"] for _, s in core.iter_sources(manifest)]
        self.assertEqual([path for path in paths if (self.vault / path).resolve().is_relative_to(own)], [])


class SameDayReplayTests(NoteCase):
    def test_a_same_day_recollect_replays_the_days_updates(self):
        morning = self.daily("2099-01-31", days=1)
        self.assertEqual(len(morning["updates"]), 2)
        self.assertEqual(self.write(dn.render(morning), morning)[0], 0)
        state = json.loads(self.state())
        first = min(morning["updates"], key=lambda update: update["sequence"])["id"]
        self.assertEqual(state["replay"], {"day": "2099-01-31", "daily": {}, "first": {"status-ledger": first}})
        again = self.daily("2099-01-31", days=1)
        self.assertEqual([u["id"] for u in again["updates"]], [u["id"] for u in morning["updates"]])
        self.assertEqual(self.daily("2099-02-01")["updates"], [])

    def test_rows_appended_or_edited_later_that_day_still_write(self):
        ledger = self.vault / "personal/status-tracker.md"
        rows = UPDATE_LEDGER.splitlines(keepends=True)
        ledger.write_text("".join(rows[:-1]))
        first = self.daily("2099-01-31", days=1)
        self.assertEqual(self.write(dn.render(first), first)[0], 0)
        ledger.write_text("".join(rows[:-1]).replace("Keep monitoring", "Keep monitoring closely") + rows[-1])
        later = self.daily("2099-01-31", days=1)
        self.assertEqual(len(later["updates"]), 2)
        code, err = self.write(dn.render(later, {"schema": 1}), later)
        self.assertEqual(code, 0, err)
        self.assertNotIn("cannot order", err)
        cursor = json.loads(self.state())["daily"]["status-ledger"]
        self.assertEqual(cursor, later["updates"][-1]["id"])
        self.assertEqual(self.daily("2099-02-01")["updates"], [])

    def test_editing_the_row_the_day_started_from_keeps_the_days_rows(self):
        """Yesterday's last row (today's replay start) edited today: the re-render keeps today's rows."""
        ledger = self.vault / "personal/status-tracker.md"
        rows = UPDATE_LEDGER.splitlines(keepends=True)
        today = rows[-1].replace("| 2099-01-30 |", "| 2099-01-31 |", 1)
        ledger.write_text("".join(rows[:-1]) + today)
        before = self.daily("2099-01-30", days=1)
        self.assertEqual(self.write(dn.render(before), before)[0], 0)
        morning = self.daily("2099-01-31", days=1)
        self.assertEqual(len(morning["updates"]), 1)
        self.assertEqual(self.write(dn.render(morning), morning)[0], 0)
        ledger.write_text("".join(rows[:-1]).replace("Keep monitoring", "Keep monitoring (fixed)") + today)
        again = self.daily("2099-01-31", days=1)
        self.assertEqual([u["id"] for u in again["updates"]], [u["id"] for u in morning["updates"]])
        self.assertEqual(again["update_warnings"], [])
        code, err = self.write(dn.render(again, {"schema": 1}), again)
        self.assertEqual(code, 0, err)
        self.assertIn("## 状态更新 · 1", self.note(morning).read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.state())["daily"]["status-ledger"], morning["updates"][0]["id"])
        self.assertEqual(self.daily("2099-02-01")["updates"], [])

    def test_state_without_replay_or_notes_needs_no_migration(self):
        path = self.vault / rc.DIGEST_UPDATES_STATE
        path.write_text(json.dumps({"schema": 1, "daily": {}, "delivered": {}, "todo_reminders": {}}))
        manifest = self.daily("2099-01-31", days=1)
        self.assertEqual(self.write(dn.render(manifest), manifest)[0], 0)
        state = json.loads(path.read_text())
        self.assertEqual(set(state), {"schema", "daily", "replay", "delivered", "todo_reminders", "notes"})
        self.assertEqual(state["schema"], 1)
        self.assertEqual(rc.load_update_state(self.vault)[1], [])

    def test_a_weekly_write_on_a_fresh_vault_leaves_daily_state_readable(self):
        self.assertEqual(self.write(dn.render(self.manifest), self.manifest)[0], 0)
        state = json.loads(self.state())
        self.assertEqual((state["daily"], state["delivered"], state["todo_reminders"]), ({}, {}, {}))
        self.assertIn("inbox/digest/2099-01/2099-01-30-weekly-digest.md", state["notes"])
        self.assertEqual(len(self.daily("2099-01-31", days=1)["updates"]), 2)

    def test_an_earlier_days_rerender_never_drops_its_updates(self):
        day = {**self.daily("2099-01-31", days=1), "generated": "2099-01-31T06:20:00"}
        self.assertEqual(self.write(dn.render(day), day)[0], 0)
        self.assertIn("## 状态更新 · 2", self.note(day).read_text())
        after = {**self.daily("2099-02-01"), "generated": "2099-02-01T06:20:00"}
        self.assertEqual(self.write(dn.render(after), after)[0], 0)
        again = {**self.daily("2099-01-31", days=1), "generated": "2099-02-01T09:00:00"}
        with contextlib.suppress(SystemExit):  # refusing the write would also keep the rows
            self.write(dn.render(again, {"schema": 1, "headline": "x"}), again)
        self.assertIn("## 状态更新 · 2", self.note(day).read_text())


class CompareAndSwapTests(NoteCase):
    """A note is replaced only while it holds what the harness wrote, or with approval."""

    def setUp(self):
        super().setUp()
        self.manifest = self.daily("2099-01-31", days=1)
        self.target = self.note(self.manifest)
        self.assertEqual(self.write(dn.render(self.manifest), self.manifest)[0], 0)

    def test_the_harness_note_is_replaced_but_an_edited_note_is_refused(self):
        curated = dn.render(self.manifest, {"schema": 1, "headline": "h"})
        self.assertEqual(self.write(curated, self.manifest)[0], 0)
        self.assertIn("curated: true", self.target.read_text())
        self.target.write_text(self.target.read_text() + "\nmy own line\n")
        before, edited = self.state(), self.target.read_bytes()
        code, err = self.write(dn.render(self.manifest), self.manifest)
        self.assertEqual((code, self.target.read_bytes(), self.state()), (rd.REFUSED_EXIT, edited, before))
        sha = rd._sha(edited)
        self.assertIn(f"--replace {sha}", err)
        self.assertEqual(self.write(dn.render(self.manifest), self.manifest, replace="0" * 64)[0], rd.REFUSED_EXIT)
        self.assertEqual((self.target.read_bytes(), self.state()), (edited, before))
        self.assertEqual(self.write(dn.render(self.manifest), self.manifest, replace=sha)[0], 0)
        self.assertNotIn("my own line", self.target.read_text())

    def test_identical_bytes_keep_the_mtime_and_rewrite_state(self):
        rel = self.target.relative_to(self.vault).as_posix()
        path = self.vault / rc.DIGEST_UPDATES_STATE
        state = json.loads(path.read_text())
        state["notes"][rel]["generated"] = ""
        path.write_text(json.dumps(state))
        before = self.target.stat().st_mtime_ns
        self.assertEqual(self.write(dn.render(self.manifest), self.manifest)[0], 0)
        self.assertEqual(self.target.stat().st_mtime_ns, before)
        self.assertEqual(json.loads(path.read_text())["notes"][rel],
                         {"sha256": rd._sha(self.target.read_bytes()), "generated": self.manifest["generated"]})

    def test_an_older_collection_never_replaces_a_newer_note(self):
        newer = {**self.manifest, "generated": "2099-01-31T12:00:00"}
        self.assertEqual(self.write(dn.render(newer, {"schema": 1}), newer)[0], 0)
        note, state = self.target.read_bytes(), self.state()
        older = {**self.manifest, "generated": "2099-01-31T09:00:00"}
        for replace in ("", rd._sha(note)):
            with self.subTest(replace=bool(replace)):
                with self.assertRaisesRegex(SystemExit, "newer collection"):
                    self.write(dn.render(older), older, replace=replace)
                self.assertEqual((self.target.read_bytes(), self.state()), (note, state))

    def test_reflect_frontmatter_survives_every_rewrite(self):
        """Every key Reflect owns outlives an approved replace and the next rewrite; dropping `private: true`
        would open a note the user hid from external AI."""
        keys = ("id: 01J0000000000000000000000Z\npinned: true\nprivate: true\naliases:\n  - Old\n"
                "gist: kept\nignoredContacts:\n  - someone\n")
        text = self.target.read_text().replace("curated: false\n", "curated: false\n" + keys, 1)
        self.target.write_text(text)
        code, _ = self.write(dn.render(self.manifest, {"schema": 1}), self.manifest, replace=rd._sha(text))
        self.assertEqual(code, 0)
        first = self.target.read_text()
        self.assertTrue(first.startswith("---\ncurated: true\n" + keys + "---\n\n# "))
        rel = self.target.relative_to(self.vault).as_posix()
        self.assertEqual(json.loads(self.state())["notes"][rel]["sha256"], rd._sha(first))
        self.assertEqual(self.write(dn.render(self.manifest, {"schema": 1, "headline": "x"}), self.manifest)[0], 0)
        self.assertIn("\n" + keys + "---\n", self.target.read_text())
        self.assertEqual(nonnative(self.target.read_text()), Counter())

    def test_a_pin_toggled_in_reflect_does_not_block_an_identical_render(self):
        pinned = self.target.read_text().replace("curated: false\n", "curated: false\npinned: true\n", 1)
        self.target.write_text(pinned)
        code, err = self.write(dn.render(self.manifest), self.manifest)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.target.read_text(), pinned)
        rel = self.target.relative_to(self.vault).as_posix()
        self.assertEqual(json.loads(self.state())["notes"][rel]["sha256"], rd._sha(pinned))

    def test_a_zero_indent_alias_list_survives_an_approved_replace(self):
        """`aliases:\\n- Old name` is valid YAML (PyYAML writes it); the replace keeps `aliases:` but drops the item."""
        keys = "aliases:\n- Old name\npinned: true\n"
        text = self.target.read_text().replace("curated: false\n", "curated: false\n" + keys, 1)
        self.target.write_text(text)
        code, err = self.write(dn.render(self.manifest, {"schema": 1}), self.manifest, replace=rd._sha(text))
        self.assertEqual(code, 0, err)
        self.assertTrue(self.target.read_text().startswith("---\ncurated: true\n" + keys + "---\n\n# "))

    def test_a_note_that_is_not_utf8_is_refused_without_a_traceback(self):
        self.target.write_bytes(b"\xff\xfe broken")
        before = self.state()
        for replace in ("", rd._sha(b"\xff\xfe broken")):
            with self.subTest(replace=bool(replace)):
                code, err = self.write(dn.render(self.manifest, {"schema": 1}), self.manifest, replace=replace)
                self.assertEqual(code, rd.REFUSED_EXIT)
                self.assertIn("is not UTF-8 text; inspect it or move it aside, then write again", err)
                self.assertNotIn("--replace", err)
                self.assertEqual((self.target.read_bytes(), self.state()), (b"\xff\xfe broken", before))

    def test_previews_record_nothing(self):
        self.target.unlink()
        (self.vault / rc.DIGEST_UPDATES_STATE).unlink()
        out = Path(self.tmp.name) / "note.md"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(rd.write(self.vault, "# x\n", self.manifest, out=out), 0)
            self.assertEqual(rd.write(self.vault, "# x\n", self.manifest, dry_run=True), 0)
        self.assertIn("sha256 " + rd._sha("# x\n"), buffer.getvalue())
        self.assertEqual((out.read_text(), self.state(), self.target.exists()), ("# x\n", None, False))
        with self.assertRaisesRegex(SystemExit, "outside"):
            rd.write(self.vault, "# x\n", self.manifest, out=self.vault / "inbox/preview.md")
        self.assertFalse((self.vault / "inbox/preview.md").exists())

    def test_the_write_must_match_the_approved_preview(self):
        text = dn.render(self.manifest, {"schema": 1})
        before, note = self.state(), self.target.read_bytes()
        self.assertEqual(self.write(text, self.manifest, expect=rd._sha(text + "x"))[0], rd.REFUSED_EXIT)
        self.assertEqual((self.target.read_bytes(), self.state()), (note, before))
        self.assertEqual(self.write(text, self.manifest, expect=rd._sha(text))[0], 0)
        self.assertEqual(self.target.read_text(), text)

    def test_the_scheduled_mode_never_replaces(self):
        before, note = self.state(), self.target.read_bytes()
        self.assertEqual(self.write("# other\n", self.manifest, create_only=True)[0], 0)
        self.assertEqual((self.target.read_bytes(), self.state()), (note, before))

    def test_weekly_note_is_recorded_and_rewritable(self):
        weekly = rc.collect(self.vault, mode="weekly", until="2099-01-30")
        target = self.note(weekly)
        rel = target.relative_to(self.vault).as_posix()
        daily = json.loads(self.state())["daily"]
        self.assertEqual(self.write(dn.render(weekly), weekly)[0], 0)
        self.assertEqual(json.loads(self.state())["notes"][rel]["sha256"], rd._sha(target.read_bytes()))
        curated = dn.render(weekly, {"schema": 1, "headline": "一周一句话"})
        self.assertEqual(self.write(curated, weekly)[0], 0)
        self.assertEqual(target.read_text(encoding="utf-8"), curated)
        state = json.loads(self.state())
        self.assertEqual(state["notes"][rel]["sha256"], rd._sha(curated))
        self.assertEqual(state["daily"], daily)

    def test_a_note_the_harness_never_recorded_is_refused(self):
        """A hand-made note, or one whose record was lost or pruned, is the user's until they approve replacing it."""
        other = self.daily("2099-01-30")
        target = self.note(other)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# A note put here by hand\n", encoding="utf-8")
        before = self.state()
        code, err = self.write(dn.render(other), other)
        self.assertEqual(code, rd.REFUSED_EXIT)
        self.assertIn(f"--replace {rd._sha(target.read_bytes())}", err)
        self.assertEqual((target.read_text(), self.state()), ("# A note put here by hand\n", before))

    def test_a_note_edited_during_the_write_is_kept_and_nothing_recorded(self):
        real, before = rd.atomic_write, self.state()

        def edit_first(path, text, **kwargs):
            if Path(path) == self.target:  # a Reflect save lands between the compare and the replace
                self.target.write_text(self.target.read_text() + "\nedited in Reflect\n")
            return real(path, text, **kwargs)

        with patch.object(rd, "atomic_write", side_effect=edit_first):
            code, err = self.write(dn.render(self.manifest, {"schema": 1}), self.manifest)
        self.assertEqual(code, rd.REFUSED_EXIT, err)
        self.assertIn("changed during the write; nothing recorded", err)
        self.assertIn("edited in Reflect", self.target.read_text())
        self.assertEqual(self.state(), before)

    def test_a_failed_note_write_records_nothing(self):
        other = self.daily("2099-01-30")
        target, real, before = self.note(other), rd.atomic_write, self.state()

        def fail_on_the_note(path, text, **kwargs):
            if Path(path) == target:
                raise OSError("disk full")
            return real(path, text, **kwargs)

        with patch.object(rd, "atomic_write", side_effect=fail_on_the_note), self.assertRaises(OSError):
            self.write(dn.render(other), other)
        self.assertFalse(target.exists())
        self.assertEqual(self.state(), before)

    def test_other_notes_keep_their_records_until_two_weeks_pass(self):
        earlier = self.daily("2099-01-30")
        weekly = rc.collect(self.vault, mode="weekly", until="2099-01-30")
        for manifest in (earlier, weekly):
            self.assertEqual(self.write(dn.render(manifest), manifest)[0], 0)
        # Each curated re-render still finds the record its note was written with.
        for manifest in (self.manifest, weekly):
            with self.subTest(note=self.note(manifest).name):
                code, err = self.write(dn.render(manifest, {"schema": 1}), manifest)
                self.assertEqual(code, 0, err)
        # An earlier daily note is history once a later one exists, and keeps its record.
        with self.assertRaisesRegex(SystemExit, "predates the 2099-01-31 note"):
            self.write(dn.render(earlier, {"schema": 1}), earlier)
        self.assertIn(self.note(earlier).relative_to(self.vault).as_posix(), json.loads(self.state())["notes"])
        late = self.daily("2099-02-20")
        self.assertEqual(self.write(dn.render(late), late)[0], 0)
        self.assertEqual(set(json.loads(self.state())["notes"]), {"inbox/digest/2099-02/2099-02-20-daily-digest.md"})

    def test_a_brief_or_context_for_another_day_is_refused(self):
        note, before = self.target.read_bytes(), self.state()
        curated = dn.render(self.manifest, {"schema": 1})
        brief = self._brief({"text": "x", "days_left": 3})  # dated 2099-01-30, a day before the note
        with self.assertRaisesRegex(SystemExit, "brief date differs"):
            self.write(curated, self.manifest, brief=brief)
        with self.assertRaisesRegex(SystemExit, "context date differs"):
            self.write(curated, self.manifest, context={"schema": 1, "date": "2099-01-30"})
        self.assertEqual((self.target.read_bytes(), self.state()), (note, before))

    def test_a_preview_through_a_symlink_into_the_vault_is_refused(self):
        link = Path(self.tmp.name) / "vault-link"
        link.symlink_to(self.vault / "inbox", target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, "outside"):
            rd.write(self.vault, "# x\n", self.manifest, out=link / "preview.md")
        self.assertFalse((self.vault / "inbox/preview.md").exists())

    def test_a_preview_needs_no_readable_update_configuration(self):
        (self.vault / rc.DIGEST_UPDATES_CONFIG).write_text("[broken")
        text, note, before = dn.render(self.manifest, {"schema": 1}), self.target.read_bytes(), self.state()
        out = Path(self.tmp.name) / "preview.md"
        self.assertEqual(self.write(text, self.manifest, out=out)[0], 0)
        self.assertEqual(self.write(text, self.manifest, dry_run=True)[0], 0)
        self.assertEqual((out.read_text(), self.target.read_bytes(), self.state()), (text, note, before))
        with self.assertRaisesRegex(SystemExit, "config unreadable"):
            self.write(text, self.manifest)

    def test_write_names_non_native_syntax_but_still_writes(self):
        text = "---\ncurated: false\n---\n\n# x\n\n<b>bold</b>\n"
        code, err = self.write(text, self.manifest)
        self.assertEqual(code, 0, err)
        self.assertIn("warning: 2 non-native html in the note", err)
        self.assertEqual(self.target.read_text(), text)


class MorningTests(NoteCase):
    """The scheduled, model-free note: create-if-absent for the effective day, one JSON line out."""

    def run_morning(self, now: datetime, **kwargs) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rd.morning(self.vault, now=now, **kwargs)
        return code, out.getvalue()

    def test_creates_once_for_the_effective_day_then_exits_before_any_input(self):
        import daily_context

        calls = []
        real = daily_context.build

        def spy(day, **kwargs):
            calls.append((day, kwargs))
            print("WARN: gtd/private.md:3: owner moved", file=sys.stderr)
            return real(day, **{**kwargs, "offline": True})

        with patch.object(daily_context, "build", side_effect=spy):
            code, out = self.run_morning(datetime(2099, 2, 1, 2, 30), no_weather=True)
            self.assertEqual(code, 0)
            summary = json.loads(out)
            self.assertEqual(summary["status"], "wrote")
            self.assertEqual(summary["note"], "$OV/inbox/digest/2099-01/2099-01-31-daily-digest.md")
            self.assertEqual(set(summary), {"status", "note", "files", "updates", "groups", "nonnative"})
            self.assertEqual(summary["nonnative"], 0)
            self.assertEqual(calls[0][0], date(2099, 1, 31))
            self.assertEqual((calls[0][1]["place"], calls[0][1]["no_weather"]), (None, True))
            note = (self.vault / "inbox/digest/2099-01/2099-01-31-daily-digest.md").read_text()
            self.assertTrue(note.startswith("---\ncurated: false\n---\n\n# Atelier Daily: 2099-01-31\n"))
            self.assertIn("owner moved", note.split("**输入缺口**", 1)[1])
            self.assertNotIn("private.md", out)
            state = self.state()
            code, out = self.run_morning(datetime(2099, 1, 31, 23, 0), no_weather=True)
            self.assertEqual((code, json.loads(out)["status"], len(calls), self.state()), (0, "exists", 1, state))

    def test_a_note_without_a_recorded_write_fails_visibly(self):
        """A retry after a lost state write must not report success over an unrecorded note."""
        target = core.note_path(self.vault, {"mode": "daily", "window": {"until": "2099-01-31"}})
        target.parent.mkdir(parents=True)
        target.write_text("# Atelier Daily: 2099-01-31\n", encoding="utf-8")
        code, out = self.run_morning(datetime(2099, 1, 31, 9, 0), no_weather=True)
        self.assertEqual((code, json.loads(out)["status"]), (1, "unrecorded"))
        self.assertEqual(target.read_text(encoding="utf-8"), "# Atelier Daily: 2099-01-31\n")
        self.assertIsNone(self.state())

    def test_an_empty_window_writes_nothing_and_never_refreshes_quota(self):
        import daily_brief
        import daily_context

        empty = {"schema": 1, "date": "2020-01-01", "groups": [], "warnings": [], "signals": {}}
        with patch.object(daily_brief, "build", return_value=empty), patch.object(daily_context, "build") as build:
            code, out = self.run_morning(datetime(2020, 1, 1, 9, 0), refresh_quota=True)
        self.assertEqual((code, json.loads(out)["status"]), (0, "empty"))
        build.assert_not_called()
        self.assertFalse((self.vault / "inbox/digest").exists())
        self.assertIsNone(self.state())

    def test_a_failure_prints_one_line_naming_only_its_kind(self):
        with patch.object(rd, "collect", side_effect=SystemExit("routine registry missing: $OV/_tools/x")):
            code, out = self.run_morning(datetime(2099, 1, 31, 9, 0))
        self.assertEqual(code, 1)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(json.loads(out), {"status": "failed", "note": "$OV/inbox/digest/2099-01/2099-01-31-daily-digest.md",
                                           "stage": "collect", "error": "SystemExit"})
        self.assertNotIn("_tools", out)
        log = self.vault / rd.ERROR_LOG
        self.assertIn("_tools/x", log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_the_cli_writes_offline(self):
        proc = self._run("morning", "--no-weather")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(proc.stdout.splitlines()), 1)
        self.assertEqual(proc.stderr, "")
        self.assertIn(json.loads(proc.stdout)["status"], {"wrote", "empty"})

    def test_the_cli_passes_both_flags(self):
        with patch.object(rd, "morning", return_value=0) as morning:
            self.assertEqual(rd.main(["morning", "--refresh-quota", "--no-weather"]), 0)
        self.assertEqual(morning.call_args.kwargs, {"refresh_quota": True, "no_weather": True})

    def test_an_existing_note_exits_before_collect_the_brief_or_the_context(self):
        import daily_brief
        import daily_context

        manifest = self.daily("2099-01-31", days=1)  # a recorded write, as /digest or an earlier run leaves it
        self.assertEqual(self.write(dn.render(manifest), manifest)[0], 0)
        self.assertTrue((self.vault / "inbox/digest/2099-01/2099-01-31-daily-digest.md").exists())
        with patch.object(rd, "collect") as collect, patch.object(daily_brief, "build") as brief, \
                patch.object(daily_context, "build") as context:
            code, out = self.run_morning(datetime(2099, 1, 31, 9, 0), refresh_quota=True)
        self.assertEqual((code, json.loads(out)["status"]), (0, "exists"))
        for spy in (collect, brief, context):
            spy.assert_not_called()

    def test_it_refreshes_quota_when_asked_and_writes_create_only(self):
        import daily_context

        real_build, real_write, seen = daily_context.build, rd.write, {}

        def build(day, **kwargs):
            seen["refresh_quota"] = kwargs["refresh_quota"]
            return real_build(day, **{**kwargs, "offline": True})

        def write(*args, **kwargs):
            seen["create_only"] = kwargs.get("create_only")
            return real_write(*args, **kwargs)

        with patch.object(daily_context, "build", side_effect=build), patch.object(rd, "write", side_effect=write):
            code, out = self.run_morning(datetime(2099, 1, 31, 9, 0), refresh_quota=True, no_weather=True)
        self.assertEqual((code, json.loads(out)["status"]), (0, "wrote"))
        self.assertEqual(seen, {"refresh_quota": True, "create_only": True})

    def test_a_failure_names_the_stage_it_reached(self):
        import daily_brief
        import daily_context

        for stage, owner, name in (("brief", daily_brief, "build"), ("context", daily_context, "build"),
                                   ("render", rd, "render"), ("write", rd, "write")):
            with self.subTest(stage=stage), patch.object(owner, name, side_effect=RuntimeError("boom")):
                code, out = self.run_morning(datetime(2099, 1, 31, 9, 0), no_weather=True)
                self.assertEqual((code, json.loads(out)["stage"]), (1, stage))

    def test_the_error_log_keeps_helper_output_and_a_success_clears_it(self):
        import daily_brief

        def noisy(*args, **kwargs):
            print("helper said: tracker row moved", file=sys.stderr)
            raise RuntimeError("boom")

        with patch.object(daily_brief, "build", side_effect=noisy):
            code, out = self.run_morning(datetime(2099, 1, 31, 9, 0), no_weather=True)
        log = self.vault / rd.ERROR_LOG
        self.assertEqual(code, 1)
        self.assertNotIn("tracker row", out)
        self.assertIn("helper said: tracker row moved", log.read_text())
        code, out = self.run_morning(datetime(2099, 1, 31, 9, 0), no_weather=True)
        self.assertEqual((code, json.loads(out)["status"]), (0, "wrote"))
        self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
