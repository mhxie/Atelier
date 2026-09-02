"""Tests for scripts/feed_fetch.py.

Context: the news routine declared 38 channels and reached 1 on a good day and 0
on most, while still emitting a dozen items sourced from ad-hoc web searches. It
looked healthy from outside because output existed. These pin the two properties
that make that failure visible instead: both feed dialects actually parse, and a
dead channel is counted rather than swallowed.

No network. Parsing is the part worth testing; reachability is the caller's
environment.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import feed_fetch as ff  # noqa: E402

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example</title>
  <item>
    <title>Anthropic raises a round</title>
    <link>https://example.com/a</link>
    <pubDate>Mon, 31 Aug 2026 12:00:00 GMT</pubDate>
    <description>&lt;p&gt;Some &lt;b&gt;markup&lt;/b&gt; in the summary.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Old news</title>
    <link>https://example.com/old</link>
    <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <entry>
    <title>Mac mini refresh</title>
    <link href="https://example.com/mac"/>
    <updated>2026-08-31T09:00:00Z</updated>
    <summary>A new Mac mini.</summary>
  </entry>
</feed>"""


class ParseTests(unittest.TestCase):
    def test_rss_items_parse(self):
        entries = ff.parse_feed(RSS)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["title"], "Anthropic raises a round")
        self.assertEqual(entries[0]["url"], "https://example.com/a")

    def test_atom_entries_parse_with_href_links(self):
        entries = ff.parse_feed(ATOM)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], "https://example.com/mac")

    def test_markup_is_stripped_from_summaries(self):
        entries = ff.parse_feed(RSS)
        self.assertNotIn("<b>", entries[0]["summary"])
        self.assertIn("markup", entries[0]["summary"])

    def test_malformed_xml_yields_nothing_rather_than_raising(self):
        self.assertEqual(ff.parse_feed("<rss><channel>"), [])
        self.assertEqual(ff.parse_feed("not xml at all"), [])

    def test_entries_without_a_link_are_skipped(self):
        self.assertEqual(ff.parse_feed("<rss><channel><item><title>x</title></item></channel></rss>"), [])

    def test_both_date_dialects_parse(self):
        self.assertIsNotNone(ff._parse_when("Mon, 31 Aug 2026 12:00:00 GMT"))
        self.assertIsNotNone(ff._parse_when("2026-08-31T09:00:00Z"))
        self.assertIsNone(ff._parse_when("not a date"))
        self.assertIsNone(ff._parse_when(""))


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "_meta").mkdir()
        (self.vault / "_meta" / "feeds.toml").write_text(
            '[[feed]]\nurl = "https://a.example/feed"\nlabel = "A"\n\n'
            '[[feed]]\nurl = "https://b.example/feed"\nlabel = "B"\n',
            encoding="utf-8",
        )
        self.now = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def _collect(self, side_effect):
        # Healing and persistence are exercised in HealingTests. Off here so
        # these stay about windowing and counting, and touch no network.
        with mock.patch.object(ff, "fetch_one", side_effect=side_effect):
            return ff.collect(
                self.vault, days=3, now=self.now, heal=False, persist=False
            )

    def test_a_dead_channel_is_counted_not_swallowed(self):
        """The failure this whole script exists to make visible."""
        def side_effect(feed, *, timeout):
            if feed["label"] == "A":
                return {**feed, "ok": True, "error": "", "entries": ff.parse_feed(RSS)}
            return {**feed, "ok": False, "error": "HTTPError", "entries": []}

        payload = self._collect(side_effect)
        self.assertEqual(payload["channels"]["declared"], 2)
        self.assertEqual(payload["channels"]["reached"], 1)
        self.assertEqual([f["label"] for f in payload["failures"]], ["B"])

    def test_a_truncated_response_is_a_failed_channel_not_a_crash(self):
        """IncompleteRead is an http.client error, not an OSError; it used to
        escape fetch_one and abort the whole collect."""
        import http.client

        class Truncated:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *_args):
                raise http.client.IncompleteRead(b"partial")

        with mock.patch.object(ff.urllib.request, "urlopen", return_value=Truncated()):
            result = ff.fetch_one({"label": "A", "url": "https://a.example/feed"}, timeout=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "IncompleteRead")
        self.assertEqual(result["entries"], [])

    def test_items_outside_the_window_are_dropped(self):
        def side_effect(feed, *, timeout):
            return {**feed, "ok": True, "error": "", "entries": ff.parse_feed(RSS)}

        titles = {i["title"] for i in self._collect(side_effect)["items"]}
        self.assertIn("Anthropic raises a round", titles)
        self.assertNotIn("Old news", titles)

    def test_an_undated_entry_is_kept(self):
        """Plenty of feeds omit dates; dropping them silences the channel."""
        def side_effect(feed, *, timeout):
            return {**feed, "ok": True, "error": "",
                    "entries": [{"title": "t", "url": "https://x/1", "published": "", "summary": ""}]}

        self.assertEqual(len(self._collect(side_effect)["items"]), 1)

    def test_the_same_url_from_two_feeds_appears_once(self):
        def side_effect(feed, *, timeout):
            return {**feed, "ok": True, "error": "",
                    "entries": [{"title": "dup", "url": "https://x/same", "published": "", "summary": ""}]}

        self.assertEqual(len(self._collect(side_effect)["items"]), 1)

    def test_total_outage_still_reports_rather_than_failing(self):
        def side_effect(feed, *, timeout):
            return {**feed, "ok": False, "error": "URLError", "entries": []}

        payload = self._collect(side_effect)
        self.assertEqual(payload["channels"]["reached"], 0)
        self.assertEqual(payload["items"], [])
        self.assertEqual(len(payload["failures"]), 2)

    def test_a_missing_feed_list_is_refused_with_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp)
            (empty / "_meta").mkdir()
            with self.assertRaises(SystemExit) as caught:
                ff.collect(empty)
            self.assertIn("feeds.toml", str(caught.exception))

    def test_non_http_urls_are_ignored(self):
        (self.vault / "_meta" / "feeds.toml").write_text(
            '[[feed]]\nurl = "file:///etc/passwd"\nlabel = "bad"\n', encoding="utf-8"
        )
        self.assertEqual(ff.load_feeds(self.vault), [])


class HealingTests(unittest.TestCase):
    """A broken feed should repair itself or leave, not sit there dragging the number down."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "_meta").mkdir()
        (self.vault / "_meta" / "feeds.toml").write_text(
            '[[feed]]\nurl = "https://a.example/ai/feed/"\nlabel = "A"\n', encoding="utf-8"
        )
        self.now = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_candidate_urls_cover_the_common_renames(self):
        candidates = ff.heal_candidates("https://x.example/blog/feed")
        self.assertIn("https://x.example/blog/rss", candidates)
        self.assertNotIn("https://x.example/blog/feed", candidates)

    def test_http_is_retried_as_https(self):
        self.assertIn("https://x.example/feed", ff.heal_candidates("http://x.example/feed"))

    def test_a_repaired_feed_is_reported_and_remembered(self):
        healthy = {"ok": True, "error": "", "entries": ff.parse_feed(RSS)}

        def fetch(feed, *, timeout):
            if feed["url"].endswith("/ai/feed/"):
                return {**feed, "ok": False, "error": "HTTPError", "entries": []}
            return {**feed, **healthy}

        with mock.patch.object(ff, "fetch_one", side_effect=fetch):
            payload = ff.collect(self.vault, days=3, now=self.now)

        self.assertEqual(len(payload["healed"]), 1)
        self.assertEqual(payload["channels"]["reached"], 1)
        health = ff.load_health(self.vault)
        self.assertTrue(any(v.get("healed_from") for v in health.values()))

    def test_failures_accumulate_strikes(self):
        def fetch(feed, *, timeout):
            return {**feed, "ok": False, "error": "HTTPError", "entries": []}

        with mock.patch.object(ff, "fetch_one", side_effect=fetch), \
             mock.patch.object(ff, "discover_feed", return_value=""):
            first = ff.collect(self.vault, days=3, now=self.now)
            second = ff.collect(self.vault, days=3, now=self.now)

        self.assertEqual(first["failures"][0]["strikes"], 1)
        self.assertEqual(second["failures"][0]["strikes"], 2)
        self.assertEqual(second["retired"], [])

    def test_a_persistently_dead_feed_retires_itself(self):
        def fetch(feed, *, timeout):
            return {**feed, "ok": False, "error": "HTTPError", "entries": []}

        with mock.patch.object(ff, "fetch_one", side_effect=fetch), \
             mock.patch.object(ff, "discover_feed", return_value=""):
            for _ in range(ff.RETIRE_AFTER_FAILURES):
                payload = ff.collect(self.vault, days=3, now=self.now)

        self.assertEqual(len(payload["retired"]), 1)
        self.assertEqual(
            ff.load_health(self.vault)["https://a.example/ai/feed/"]["status"], "retired"
        )

    def test_a_retired_feed_leaves_the_denominator(self):
        """Keeping it would depress the number forever and hide the next regression."""
        ff.save_health(
            self.vault,
            {"https://a.example/ai/feed/": {"status": "retired", "consecutive_failures": 9}},
        )
        with mock.patch.object(ff, "fetch_one") as fetch:
            payload = ff.collect(self.vault, days=3, now=self.now)
        fetch.assert_not_called()
        self.assertEqual(payload["channels"]["declared"], 0)
        self.assertEqual(payload["channels"]["retired"], 1)

    def test_a_recovered_feed_resets_its_strikes(self):
        ff.save_health(self.vault, {"https://a.example/ai/feed/": {"consecutive_failures": 3}})

        def fetch(feed, *, timeout):
            return {**feed, "ok": True, "error": "", "entries": ff.parse_feed(RSS)}

        with mock.patch.object(ff, "fetch_one", side_effect=fetch):
            ff.collect(self.vault, days=3, now=self.now)
        self.assertEqual(
            ff.load_health(self.vault)["https://a.example/ai/feed/"]["consecutive_failures"], 0
        )

    def test_no_persist_leaves_state_untouched(self):
        def fetch(feed, *, timeout):
            return {**feed, "ok": False, "error": "HTTPError", "entries": []}

        with mock.patch.object(ff, "fetch_one", side_effect=fetch), \
             mock.patch.object(ff, "discover_feed", return_value=""):
            ff.collect(self.vault, days=3, now=self.now, persist=False)
        self.assertEqual(ff.load_health(self.vault), {})
