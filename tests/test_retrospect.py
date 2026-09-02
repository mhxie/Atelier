"""Tests for scripts/retrospect.py.

The two properties that decide whether this section survives contact with a
reader: it must not repeat itself into irrelevance, and it must not mail
something the reader would not have chosen to send. The second is the sharper
one, because the sampler weights the most personal tier highest.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrospect as rx  # noqa: E402

TODAY = date(2026, 9, 1)


def build(root: Path) -> Path:
    vault = root / "vault"
    (vault / "_meta").mkdir(parents=True)
    for tier, names in {
        "reflections": ["2025-01-02-old-thought.md", "2025-02-02-private-conflict.md"],
        "wiki": ["A Pattern.md"],
        "daily-notes": ["2025-03-03.md"],
    }.items():
        directory = vault / tier
        directory.mkdir(parents=True)
        for name in names:
            (directory / name).write_text(
                f"# {name.rsplit('.', 1)[0]}\n\nSome prose worth resurfacing later.\n",
                encoding="utf-8",
            )
    (vault / "reflections" / "2026-08-30-too-recent.md").write_text(
        "# Too recent\n\nnot a retrospective\n", encoding="utf-8"
    )
    return vault


class DrawTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_recent_notes_are_not_retrospective(self):
        paths = {p.name for p, _t, _w in rx.candidates(self.vault, TODAY)}
        self.assertNotIn("2026-08-30-too-recent.md", paths)
        self.assertIn("2025-01-02-old-thought.md", paths)

    def test_a_draw_is_recorded_and_then_excluded(self):
        first = rx.draw(self.vault, count=1, today=TODAY, seed=1)
        self.assertEqual(len(first), 1)
        rx.record(self.vault, first, TODAY)
        remaining = {p for p, _t, _w in rx.candidates(self.vault, TODAY)}
        pool = rx.draw(self.vault, count=10, today=TODAY, seed=1)
        self.assertNotIn(first[0]["path"], {p["path"] for p in pool})
        self.assertTrue(remaining)

    def test_no_record_keeps_the_note_eligible(self):
        first = rx.draw(self.vault, count=1, today=TODAY, seed=1)
        again = rx.draw(self.vault, count=10, today=TODAY, seed=1)
        self.assertIn(first[0]["path"], {p["path"] for p in again})

    def test_an_empty_pool_is_a_reportable_state_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "v"
            (empty / "_meta").mkdir(parents=True)
            self.assertEqual(rx.draw(empty, today=TODAY), [])


class ReviewGateTests(unittest.TestCase):
    """The draw leaves the machine, so nothing ships without a ruling.

    An explicit denylist was the first design and the wrong one: it made the
    user enumerate their own sensitivities in advance, which is the same
    guessing problem moved one seat over. Reviewing after the draw and caching
    the ruling costs one judgement per note, once.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build(Path(self.tmp.name))
        self.target = "reflections/2025-02-02-private-conflict.md"

    def tearDown(self):
        self.tmp.cleanup()

    def _rule(self, relative: str, verdict: str, digest: str | None = None) -> None:
        path = self.vault / relative
        rx.save_verdicts(
            self.vault,
            {relative: {"hash": digest or rx.content_hash(path), "verdict": verdict,
                        "reviewed": "2026-09-01"}},
        )

    def test_an_unreviewed_pick_is_marked_not_shippable(self):
        picks = rx.draw(self.vault, count=50, today=TODAY, seed=2)
        self.assertTrue(picks)
        self.assertTrue(all(p["reviewed"] is False for p in picks))

    def test_a_rejected_note_is_never_drawn_again(self):
        self._rule(self.target, "reject")
        drawn = {p["path"] for p in rx.draw(self.vault, count=50, today=TODAY, seed=2)}
        self.assertNotIn(self.target, drawn)

    def test_an_approved_note_comes_back_shippable(self):
        self._rule(self.target, "approve")
        picks = [p for p in rx.draw(self.vault, count=50, today=TODAY, seed=2)
                 if p["path"] == self.target]
        self.assertEqual(len(picks), 1)
        self.assertTrue(picks[0]["reviewed"])

    def test_editing_a_note_invalidates_its_ruling(self):
        """A stale approval would carry newly added text into a mail server."""
        self._rule(self.target, "approve")
        (self.vault / self.target).write_text("# changed\n\nnew text\n", encoding="utf-8")
        picks = [p for p in rx.draw(self.vault, count=50, today=TODAY, seed=2)
                 if p["path"] == self.target]
        self.assertEqual(len(picks), 1)
        self.assertFalse(picks[0]["reviewed"], "an edited note must be asked about again")

    def test_editing_also_lifts_a_stale_rejection(self):
        """The reverse case: a redacted note must not stay buried forever."""
        self._rule(self.target, "reject", digest="stale-hash")
        drawn = {p["path"] for p in rx.draw(self.vault, count=50, today=TODAY, seed=2)}
        self.assertIn(self.target, drawn)

    def test_an_unreadable_cache_fails_closed(self):
        (self.vault / "_meta" / "retrospect_verdicts.json").write_text("{[", encoding="utf-8")
        self.assertEqual(rx.draw(self.vault, count=50, today=TODAY, seed=2), [])

    def test_verdicts_round_trip_through_the_cli_path(self):
        digest = rx.content_hash(self.vault / self.target)
        payload = self.vault / "v.json"
        payload.write_text(json.dumps(
            [{"path": self.target, "content_hash": digest, "verdict": "reject"}]
        ), encoding="utf-8")
        self.assertEqual(rx.apply_verdicts(self.vault, payload), 0)
        drawn = {p["path"] for p in rx.draw(self.vault, count=50, today=TODAY, seed=2)}
        self.assertNotIn(self.target, drawn)


class CliTests(unittest.TestCase):
    def test_json_output_carries_a_schema(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            vault = build(Path(tmp))
            proc = subprocess.run(
                [sys.executable, "scripts/retrospect.py", "--json", "--no-record",
                 "--today", "2026-09-01", "--seed", "5"],
                cwd=REPO_ROOT, capture_output=True, text=True,
                env={"PATH": "/usr/bin:/bin", "OV": str(vault)}, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["schema"], 1)
