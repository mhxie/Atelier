"""Tests for scripts/deadlines.py.

The index feeds the one screen read before the day's deep work, so the bar for
every row is provenance, not plausibility. These tests pin that: a row without a
resolvable `source` is an error, a malformed row does not take the rest of the
index down with it, and a stale index reports staleness instead of presenting
month-old extraction as today's truth.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import deadlines as dl  # noqa: E402

GOOD_INDEX = """
[meta]
refreshed = 2099-01-28
max_age_days = 10

[[deadline]]
slug = "hotel-credit-1"
label = "Hotel credit #1 unspent"
due = 2099-12-31
kind = "perk"
reversible = false
source = "finance/example-tracker.md:107"
action = "book one standalone night"

[[deadline]]
slug = "document-expiry"
label = "Document expires"
due = 2099-02-01
kind = "obligation"
reversible = false
source = "travel/example-trip.md:12"

[[deadline]]
slug = "renewal-window"
label = "Renewal window open"
due = 2099-02-05
kind = "window"
reversible = true
source = "finance/example-plan.md:29"

[[deadline]]
slug = "already-handled"
label = "Done thing"
due = 2099-02-02
kind = "perk"
reversible = false
source = "finance/example-tracker.md:1"
status = "done"
"""


def write_vault(root: Path, index_text: str | None) -> Path:
    vault = root / "vault"
    (vault / "_meta").mkdir(parents=True)
    (vault / "finance").mkdir(parents=True)
    (vault / "travel").mkdir(parents=True)
    (vault / "finance" / "example-tracker.md").write_text("x\n" * 120, encoding="utf-8")
    (vault / "finance" / "example-plan.md").write_text("x\n" * 120, encoding="utf-8")
    (vault / "travel" / "example-trip.md").write_text("x\n" * 120, encoding="utf-8")
    if index_text is not None:
        (vault / "_meta" / "deadlines.toml").write_text(index_text, encoding="utf-8")
    return vault


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = write_vault(Path(self.tmp.name), GOOD_INDEX)
        self.today = date(2099, 1, 31)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_load_sorted_by_due(self):
        index = dl.load_index(self.vault, self.today)
        self.assertEqual(index.errors, [])
        self.assertEqual(
            [d.slug for d in index.deadlines],
            ["document-expiry", "already-handled", "renewal-window", "hotel-credit-1"],
        )

    def test_days_left_and_state(self):
        index = dl.load_index(self.vault, self.today)
        document = next(d for d in index.deadlines if d.slug == "document-expiry")
        self.assertEqual(document.days_left, 1)
        self.assertEqual(document.state, "soon")
        hotel_credit = next(d for d in index.deadlines if d.slug == "hotel-credit-1")
        self.assertEqual(hotel_credit.state, "later")

    def test_expired_state(self):
        index = dl.load_index(self.vault, date(2099, 2, 10))
        document = next(d for d in index.deadlines if d.slug == "document-expiry")
        self.assertEqual(document.days_left, -9)
        self.assertEqual(document.state, "expired")

    def test_open_excludes_done_rows(self):
        index = dl.load_index(self.vault, self.today)
        self.assertNotIn("already-handled", [d.slug for d in dl.open_deadlines(index)])

    def test_closing_within_is_inclusive_and_skips_done(self):
        index = dl.load_index(self.vault, self.today)
        self.assertEqual(
            [d.slug for d in dl.closing_within(index, 7)],
            ["document-expiry", "renewal-window"],
        )

    def test_reversible_window_still_counts_as_forfeitable(self):
        """A window closes whether or not the row says reversible."""
        index = dl.load_index(self.vault, self.today)
        window = next(d for d in index.deadlines if d.slug == "renewal-window")
        self.assertTrue(window.reversible)
        self.assertTrue(window.is_forfeitable())

    def test_fresh_index_is_not_stale(self):
        index = dl.load_index(self.vault, self.today)
        self.assertFalse(index.stale)
        self.assertEqual(index.age_days, 3)
        self.assertIsNone(index.warning())

    def test_stale_index_warns_with_the_lag(self):
        index = dl.load_index(self.vault, date(2099, 2, 20))
        self.assertTrue(index.stale)
        warning = index.warning()
        self.assertIn("stale 23d", warning)
        self.assertIn("limit 10d", warning)

    def test_missing_index_warns_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = write_vault(Path(tmp), None)
            index = dl.load_index(vault, self.today)
            self.assertFalse(index.exists)
            self.assertEqual(index.deadlines, [])
            self.assertIn("missing", index.warning())

    def test_unreadable_index_is_reported_not_raised(self):
        (self.vault / "_meta" / "deadlines.toml").write_text("[[deadline]\nbroken", encoding="utf-8")
        index = dl.load_index(self.vault, self.today)
        self.assertTrue(index.exists)
        self.assertEqual(index.deadlines, [])
        self.assertTrue(index.errors)

    def test_toml_date_and_string_date_both_parse(self):
        (self.vault / "_meta" / "deadlines.toml").write_text(
            """
[meta]
refreshed = 2099-01-28

[[deadline]]
slug = "as-string"
label = "String date"
due = "2099-02-03"
kind = "perk"
reversible = false
source = "travel/example-trip.md:1"
""",
            encoding="utf-8",
        )
        index = dl.load_index(self.vault, self.today)
        self.assertEqual(index.errors, [])
        self.assertEqual(index.deadlines[0].due, "2099-02-03")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.today = date(2099, 1, 31)

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, body: str) -> dl.Index:
        vault = write_vault(Path(self.tmp.name), "[meta]\nrefreshed = 2099-01-28\n" + body)
        return dl.load_index(vault, self.today)

    def test_missing_source_is_an_error(self):
        index = self._load(
            """
[[deadline]]
slug = "no-source"
label = "No provenance"
due = 2099-02-05
kind = "perk"
reversible = false
"""
        )
        self.assertTrue(any("missing source" in e for e in index.errors))

    def test_malformed_source_is_an_error(self):
        index = self._load(
            """
[[deadline]]
slug = "bad-source"
label = "Bad provenance"
due = 2099-02-05
kind = "perk"
reversible = false
source = "finance/example-tracker.md"
"""
        )
        self.assertTrue(any("not <vault-relative" in e for e in index.errors))

    def test_unknown_kind_is_an_error_but_the_row_survives(self):
        index = self._load(
            """
[[deadline]]
slug = "odd-kind"
label = "Odd"
due = 2099-02-05
kind = "vibes"
reversible = false
source = "travel/example-trip.md:1"
"""
        )
        self.assertTrue(any("kind 'vibes'" in e or 'kind "vibes"' in e for e in index.errors))
        self.assertEqual([d.slug for d in index.deadlines], ["odd-kind"])
        self.assertEqual(index.deadlines[0].kind, "obligation")

    def test_milestone_is_a_known_kind_and_never_forfeitable(self):
        index = self._load(
            """
[[deadline]]
slug = "probe-midpoint"
label = "Probe midpoint"
due = 2099-02-05
kind = "milestone"
reversible = false
source = "travel/example-trip.md:1"
"""
        )
        self.assertEqual(index.errors, [])
        row = index.deadlines[0]
        self.assertEqual(row.kind, "milestone")
        self.assertFalse(row.is_forfeitable())

    def test_unparseable_due_drops_the_row(self):
        index = self._load(
            """
[[deadline]]
slug = "bad-date"
label = "Bad date"
due = "next tuesday"
kind = "perk"
reversible = false
source = "travel/example-trip.md:1"
"""
        )
        self.assertEqual(index.deadlines, [])
        self.assertTrue(any("unparseable due" in e for e in index.errors))

    def test_missing_reversible_is_an_error_and_defaults_safe(self):
        index = self._load(
            """
[[deadline]]
slug = "no-rev"
label = "No reversible flag"
due = 2099-02-05
kind = "obligation"
source = "travel/example-trip.md:1"
"""
        )
        self.assertTrue(any("missing reversible" in e for e in index.errors))
        self.assertTrue(index.deadlines[0].reversible)

    def test_duplicate_slug_is_an_error(self):
        index = self._load(
            """
[[deadline]]
slug = "dup"
label = "First"
due = 2099-02-05
kind = "perk"
reversible = false
source = "travel/example-trip.md:1"

[[deadline]]
slug = "dup"
label = "Second"
due = 2099-02-06
kind = "perk"
reversible = false
source = "travel/example-trip.md:2"
"""
        )
        self.assertEqual(len(index.deadlines), 1)
        self.assertTrue(any("duplicate slug" in e for e in index.errors))

    def test_one_bad_row_does_not_drop_the_good_ones(self):
        index = self._load(
            """
[[deadline]]
slug = "good"
label = "Good row"
due = 2099-02-05
kind = "perk"
reversible = false
source = "travel/example-trip.md:1"

[[deadline]]
slug = "bad"
label = "Bad row"
due = "whenever"
kind = "perk"
reversible = false
source = "travel/example-trip.md:2"
"""
        )
        self.assertEqual([d.slug for d in index.deadlines], ["good"])
        self.assertTrue(index.errors)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = write_vault(Path(self.tmp.name), GOOD_INDEX)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/deadlines.py", *args],
            cwd=REPO_ROOT,
            env={**os.environ, "OV": str(self.vault)},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_list_json_carries_index_metadata(self):
        proc = self._run("list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["index"]["refreshed"], "2099-01-28")
        self.assertIn("deadlines", payload)

    def test_kind_filter(self):
        proc = self._run("list", "--kind", "window", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual([d["slug"] for d in payload["deadlines"]], ["renewal-window"])

    def test_lint_passes_on_a_clean_index(self):
        proc = self._run("lint")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("0 errors", proc.stdout)

    def test_lint_fails_when_a_source_file_does_not_exist(self):
        (self.vault / "travel" / "example-trip.md").unlink()
        proc = self._run("lint")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("source not found in vault", proc.stderr)

    def test_lint_fails_when_a_source_line_is_past_the_end_of_the_file(self):
        toml_path = self.vault / "_meta" / "deadlines.toml"
        text = toml_path.read_text(encoding="utf-8")
        bumped = text.replace('example-tracker.md:107"', 'example-tracker.md:9999"', 1)
        self.assertNotEqual(bumped, text)
        toml_path.write_text(bumped, encoding="utf-8")
        proc = self._run("lint")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("line past end of file", proc.stderr)

    def test_lint_is_quiet_when_the_index_is_absent(self):
        (self.vault / "_meta" / "deadlines.toml").unlink()
        proc = self._run("lint")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("index absent", proc.stdout)

    def test_list_survives_a_missing_index(self):
        (self.vault / "_meta" / "deadlines.toml").unlink()
        proc = self._run("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("missing", proc.stdout)




class DoneTests(unittest.TestCase):
    """`done` is the index's one write: it closes a row in place, keeps every
    other byte, and refuses evidence that does not resolve."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = write_vault(Path(self.tmp.name), GOOD_INDEX)
        self.index = self.vault / "_meta" / "deadlines.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/deadlines.py", *args],
            cwd=REPO_ROOT,
            env={**os.environ, "OV": str(self.vault)},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_done_closes_the_row_in_place_and_records_evidence(self):
        before = self.index.read_text(encoding="utf-8")
        proc = self._run("done", "hotel-credit-1", "--resolved-by", "travel/example-trip.md:5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = self.index.read_text(encoding="utf-8")
        self.assertIn('status = "done"\nresolved = ' + date.today().isoformat(), after)
        self.assertIn('resolved_by = "travel/example-trip.md:5"', after)
        # Everything but the closed block is byte-identical.
        cut = before.index('slug = "document-expiry"')
        self.assertEqual(before[cut:], after[after.index('slug = "document-expiry"'):])
        self.assertEqual(before[: before.index("[[deadline]]")], after[: after.index("[[deadline]]")])
        loaded = dl.load_index(self.vault, date(2099, 1, 31))
        self.assertEqual(loaded.errors, [])
        self.assertEqual({d.slug: d.status for d in loaded.deadlines}["hotel-credit-1"], "done")
        self.assertNotIn("hotel-credit-1", [d.slug for d in dl.open_deadlines(loaded)])
        self.assertEqual(self._run("lint").returncode, 0)

    def test_done_refuses_a_closed_row_an_unknown_slug_and_bad_evidence(self):
        before = self.index.read_text(encoding="utf-8")
        # A real file outside the vault, reachable by traversal or absolute path.
        self.outside = str(Path(self.tmp.name) / "outside.md")
        Path(self.outside).write_text("x\n", encoding="utf-8")
        for args, message in (
            (("done", "already-handled", "--resolved-by", "travel/example-trip.md:5"), "already done"),
            (("done", "no-such-row", "--resolved-by", "travel/example-trip.md:5"), "no row with slug"),
            (("done", "hotel-credit-1", "--resolved-by", "travel/missing.md:1"), "not found in vault"),
            (("done", "hotel-credit-1", "--resolved-by", "travel/example-trip.md:999"), "past end"),
            (("done", "hotel-credit-1", "--resolved-by", "not a source"), "is not <vault-relative"),
            (("done", "hotel-credit-1", "--resolved-by", "travel/example-trip.md:0"), "1-based"),
            (("done", "hotel-credit-1", "--resolved-by", "../outside.md:1"), "outside the vault"),
            (("done", "hotel-credit-1", "--resolved-by", f"{self.outside}:1"), "outside the vault"),
        ):
            with self.subTest(args=args):
                proc = self._run(*args)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn(message, proc.stderr)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_done_rewrites_the_whole_table_even_when_status_precedes_the_slug(self):
        """A `status = "open"` written above `slug` must be replaced, not joined
        by a second `status` that leaves the index unparseable."""
        reordered = GOOD_INDEX.replace(
            '[[deadline]]\nslug = "hotel-credit-1"\n',
            '[[deadline]]\nstatus = "open"\nresolved = 2098-01-01\nslug = "hotel-credit-1"\n',
        )
        self.assertNotEqual(reordered, GOOD_INDEX)
        self.index.write_text(reordered, encoding="utf-8")
        proc = self._run("done", "hotel-credit-1", "--resolved-by", "travel/example-trip.md:5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = self.index.read_text(encoding="utf-8")
        block = after[after.index('[[deadline]]\nslug = "hotel-credit-1"') : after.index('slug = "document-expiry"')]
        self.assertEqual(block.count("status ="), 1)
        self.assertEqual(block.count("resolved ="), 1)
        self.assertIn('status = "done"', block)
        self.assertNotIn("2098-01-01", block)
        loaded = dl.load_index(self.vault, date(2099, 1, 31))
        self.assertEqual(loaded.errors, [])
        self.assertEqual({d.slug: d.status for d in loaded.deadlines}["hotel-credit-1"], "done")

    def test_done_refuses_an_index_that_already_fails_lint(self):
        """Otherwise the row is marked done and the command still exits 1,
        because the reload cannot separate old errors from this edit's."""
        broken = GOOD_INDEX.replace('kind = "window"\nreversible = true\n', 'kind = "window"\n')
        self.assertNotEqual(broken, GOOD_INDEX)
        self.index.write_text(broken, encoding="utf-8")
        proc = self._run("done", "hotel-credit-1", "--resolved-by", "travel/example-trip.md:5")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("before done", proc.stderr)
        self.assertIn("missing reversible", proc.stderr)
        self.assertEqual(self.index.read_text(encoding="utf-8"), broken)

    def test_done_without_evidence_is_refused_before_any_write(self):
        before = self.index.read_text(encoding="utf-8")
        proc = self._run("done", "hotel-credit-1")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("--resolved-by", proc.stderr)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_done_keeps_a_private_index_private(self):
        self.index.chmod(0o600)
        proc = self._run("done", "hotel-credit-1", "--resolved-by", "travel/example-trip.md:5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.index.parent.iterdir()], ["deadlines.toml"])

    def test_done_refuses_an_index_whose_sources_do_not_resolve(self):
        """`lint` reports a source file that is missing; `done` must hold the
        same bar rather than editing what lint would reject."""
        (self.vault / "finance" / "example-plan.md").unlink()
        before = self.index.read_text(encoding="utf-8")
        proc = self._run("done", "hotel-credit-1", "--resolved-by", "travel/example-trip.md:5")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("renewal-window: source not found", proc.stderr)
        self.assertIn("before done", proc.stderr)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)

    def test_done_dry_run_shows_the_edit_and_writes_nothing(self):
        before = self.index.read_text(encoding="utf-8")
        proc = self._run("done", "hotel-credit-1", "--resolved-by", "travel/example-trip.md:5", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('status = "done"', proc.stdout)
        self.assertIn("no write", proc.stdout)
        self.assertEqual(self.index.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
