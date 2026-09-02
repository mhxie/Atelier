"""interests.py: events in, strength and status out, sources deduped."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import interests as ix  # noqa: E402

TODAY = date(2099, 3, 1)

EXPERIENCE_LOG = """# Live Events

## Live Events

| Date | Event | City | Category | Credit | Notes |
|---|---|---|---|---|---|
| 2099-02-20 | Example Band: Endless Tour | Example City | Concert | | |
| 2099-01-15 | Example Idol 个人演唱会 | Example City | Concert | | |
| [2098-11-05](../daily-notes/2098/11/2098-11-05.md) | Example Team vs Other | Example City | NBA | | |
| 2097 | Example Fest | Example City | 音乐节 | | |
| 2098-06 | Example Park | Example City | Trip | | |
| not-a-date | Broken Row | | Concert | | |

### Upcoming

## Trips

| Date | Event | City | Category | Credit | Notes |
|---|---|---|---|---|---|
| 2099-01-02 | Example Museum | Example City | Museum | | |
"""

TRACKING_CACHE = {
    "schema": 1,
    "refreshed_at": "2099-02-28T06:00:00",
    "anime": {
        "last_success_at": "2099-02-28T06:00:00",
        "library": [
            {"id": 1, "title": "Example Series A", "status": "COMPLETED", "progress": 12},
            {"id": 2, "title": "Example Series B", "status": "CURRENT", "progress": 3},
            {"id": 3, "title": "Example Series C", "status": "PLANNING", "progress": 0},
            {"id": 4, "title": "Example Series D", "status": "CURRENT", "progress": 0},
        ],
    },
}

NOTE = """## 2099-02-25

早上跑步。晚上看了《Example Series E》第三集，然后去了 Example Singer 的演唱会。
周末读完了《Example Book》。玩了 Example Game 两小时。去了 Example Bistro 吃饭。
"""


def build_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "_meta").mkdir(parents=True)
    (vault / "logs").mkdir()
    (vault / "logs" / "live-events.md").write_text(EXPERIENCE_LOG, encoding="utf-8")
    cache = root / "hi-tracking.json"
    cache.write_text(json.dumps(TRACKING_CACHE), encoding="utf-8")
    (vault / "_meta" / "brief_sources.toml").write_text(f'[tracking]\ncache = "{cache}"\n', encoding="utf-8")
    (vault / "_meta" / "digest.toml").write_text('[interests]\nexperience_log = "logs/live-events.md"\n', encoding="utf-8")
    notes = vault / "daily-notes" / "2099" / "02"
    notes.mkdir(parents=True)
    (notes / "2099-02-25.md").write_text(NOTE, encoding="utf-8")
    return vault


class StrengthTests(unittest.TestCase):
    def test_weights_decay_with_a_ninety_day_half_life(self):
        it = ix.Interest(slug="x", name="X", kind="artist")
        it.events.append(ix.Event("2099-03-01", "attended"))
        self.assertAlmostEqual(it.strength(TODAY), 3.0, places=2)
        it.events = [ix.Event("2098-12-01", "attended")]  # 90 days earlier
        self.assertAlmostEqual(it.strength(TODAY), 1.5, places=2)

    def test_status_follows_strength_and_declared_never_drops_below_watch(self):
        fresh = ix.Interest(slug="a", name="A", events=[ix.Event("2099-02-28", "watched")])
        self.assertEqual(fresh.derived_status(TODAY), "active")
        faded = ix.Interest(slug="b", name="B", events=[ix.Event("2097-01-01", "watched")])
        self.assertEqual(faded.derived_status(TODAY), "dormant")
        declared = ix.Interest(slug="c", name="C", declared=True, events=[ix.Event("2097-01-01", "declared")])
        self.assertEqual(declared.derived_status(TODAY), "watch")
        declined = ix.Interest(slug="d", name="D", status="declined", events=[ix.Event("2099-02-28", "attended")])
        self.assertEqual(declined.derived_status(TODAY), "declined")

    def test_a_new_event_reactivates_a_dormant_interest(self):
        it = ix.Interest(slug="e", name="E", events=[ix.Event("2097-01-01", "watched")])
        self.assertEqual(it.derived_status(TODAY), "dormant")
        it.events.append(ix.Event("2099-02-27", "completed"))
        self.assertEqual(it.derived_status(TODAY), "active")


class LedgerRoundTripTests(unittest.TestCase):
    def test_dump_and_load_are_inverse(self):
        items = [
            ix.Interest(slug="example-band", name="Example Band", kind="artist", aliases=["EB"], declared=True,
                        events=[ix.Event("2099-02-20", "attended", "experience-log", "experience-log:2099-02-20")]),
            ix.Interest(slug="示例作品", name="示例作品", kind="anime", notes="quote \"here\""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "interests.toml"
            path.write_text(ix.dump_ledger(items), encoding="utf-8")
            back = ix.load_ledger(path)
        self.assertEqual([i.slug for i in back], ["example-band", "示例作品"])
        self.assertEqual(back[0].aliases, ["EB"])
        self.assertTrue(back[0].declared)
        self.assertEqual(back[0].events[0].ref, "experience-log:2099-02-20")
        self.assertEqual(back[1].notes, 'quote "here"')

    def test_find_matches_slug_name_and_alias_case_insensitively(self):
        items = [ix.Interest(slug="example-band", name="Example Band", aliases=["EB"])]
        self.assertIs(ix.find(items, "example band"), items[0])
        self.assertIs(ix.find(items, "eb"), items[0])
        self.assertIsNone(ix.find(items, "Other"))


class IngestTests(unittest.TestCase):
    def test_anilist_baseline_then_changes_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            items: list[ix.Interest] = []
            state: dict = {}
            notes = ix.ingest_anilist(vault, items, TODAY, state)
            self.assertIn("baseline recorded, 1 new events", notes[0])
            self.assertEqual([i.name for i in items], ["Example Series B"])
            self.assertEqual(items[0].events[0].kind, "watched")
            # Unchanged library: nothing.
            self.assertIn("0 new events", ix.ingest_anilist(vault, items, TODAY, state)[0])
            # B finishes, C starts, A stays completed: two events, A still absent.
            cache = json.loads((Path(tmp) / "hi-tracking.json").read_text())
            lib = cache["anime"]["library"]
            lib[1]["status"] = "COMPLETED"
            lib[2]["status"] = "CURRENT"
            lib[2]["progress"] = 1
            cache["anime"]["last_success_at"] = "2099-03-10T06:00:00"
            (Path(tmp) / "hi-tracking.json").write_text(json.dumps(cache))
            notes = ix.ingest_anilist(vault, items, TODAY, state)
            self.assertIn("2 new events", notes[0])
            by_name = {i.name: i for i in items}
            self.assertEqual(by_name["Example Series B"].events[-1].kind, "completed")
            self.assertEqual(by_name["Example Series C"].events[0].kind, "started")
            self.assertNotIn("Example Series A", by_name)

    def test_experience_log_rows_map_categories_and_skip_bad_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            items: list[ix.Interest] = []
            pending: list[ix.Pending] = []
            notes = ix.ingest_experience_log(vault, items, TODAY, pending)
            self.assertIn("3 new events", notes[0])
            self.assertIn("1 queued for attribution", notes[0])
            self.assertIn("2 skipped", notes[0])  # the trip row and the undated row
            self.assertFalse(any("Museum" in i.name for i in items))
            self.assertFalse(any("Park" in i.name for i in items))
            by_name = {i.name: i for i in items}
            self.assertEqual(by_name["Example Band"].kind, "artist")
            self.assertEqual(by_name["Example Idol"].kind, "artist")
            # A game names two sides and maybe neither was the reason: it waits.
            self.assertNotIn("Example Team vs Other", by_name)
            self.assertEqual([p.title for p in pending], ["Example Team vs Other"])
            self.assertEqual(by_name["Example Fest"].kind, "festival")
            self.assertEqual(by_name["Example Fest"].events[0].date, "2097-01-01")
            self.assertNotIn("Broken Row", by_name)
            # Re-ingest is idempotent even though attended events are not window-deduped.
            ix.ingest_experience_log(vault, items, TODAY, pending)
            self.assertEqual(len(by_name["Example Band"].events), 1)
            self.assertEqual(len(pending), 1)

    def test_a_game_attributes_itself_only_to_a_known_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            items = [ix.Interest(slug="example-team", name="Example Team", kind="team", declared=True)]
            pending: list[ix.Pending] = []
            ix.ingest_experience_log(vault, items, TODAY, pending)
            self.assertEqual(pending, [])
            team = ix.find(items, "Example Team")
            self.assertEqual([e.kind for e in team.events], ["attended"])
            self.assertEqual(team.events[0].date, "2098-11-05")

    def test_diary_candidates_are_curated_not_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            rows = ix.note_candidates(vault, TODAY, days=10)
            texts = " ".join(r["text"] for r in rows)
            # Both the concert line and the dining line surface: the script does
            # not know which one is about an interest, and must not pretend to.
            self.assertIn("Example Singer", texts)
            self.assertIn("Example Bistro", texts)
            self.assertTrue(all(r["ref"].startswith("daily-notes/") for r in rows))
            self.assertEqual(ix.note_candidates(vault, TODAY, days=2), [])

    def test_readwise_books_only_inside_the_window(self):
        rows = [
            {"book_id": 1, "book_title": "Example Book", "book_category": "books", "highlighted_at": "2099-02-27T10:00:00"},
            {"book_id": 2, "book_title": "Old Book", "book_category": "books", "highlighted_at": "2098-01-01T10:00:00"},
            {"book_id": 3, "book_title": "Some Article", "book_category": "articles", "highlighted_at": "2099-02-27T10:00:00"},
        ]
        items: list[ix.Interest] = []
        notes = ix.ingest_readwise(items, TODAY, 30, runner=lambda days: rows)
        self.assertIn("1 new book events", notes[0])
        self.assertEqual([i.name for i in items], ["Example Book"])

    def test_readwise_cli_absence_is_a_note(self):
        def missing(days):
            raise FileNotFoundError("readwise")

        self.assertEqual(ix.ingest_readwise([], TODAY, 30, runner=missing), ["readwise: CLI not installed"])


class CliTests(unittest.TestCase):
    def _run(self, vault: Path, *args: str) -> subprocess.CompletedProcess:
        script = Path(__file__).resolve().parents[1] / "scripts" / "interests.py"
        return subprocess.run(
            [sys.executable, str(script), "--ov", str(vault), "--today", TODAY.isoformat(), *args],
            capture_output=True,
            text=True,
        )

    def test_add_list_active_decline(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            proc = self._run(vault, "add", "--name", "Example Player", "--kind", "player", "--declared")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("recorded Example Player (player) declared", proc.stdout)
            proc = self._run(vault, "active", "--json")
            data = json.loads(proc.stdout)
            self.assertEqual(data["interests"][0]["name"], "Example Player")
            self.assertEqual(data["interests"][0]["status"], "active")
            proc = self._run(vault, "decline", "example-player")
            self.assertIn("declined", proc.stdout)
            data = json.loads(self._run(vault, "active", "--json").stdout)
            self.assertEqual(data["interests"], [])
            self.assertEqual(data["declined"], ["Example Player"])

    def test_ingest_writes_the_ledger_and_dry_run_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            proc = self._run(vault, "ingest", "--source", "experience-log", "--dry-run")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((vault / "_meta" / "interests.toml").exists())
            proc = self._run(vault, "ingest", "--source", "experience-log")
            self.assertTrue((vault / "_meta" / "interests.toml").exists())
            ledger = ix.load_ledger(vault / "_meta" / "interests.toml")
            self.assertEqual(len(ledger), 3)
            self.assertEqual(len(ix.load_pending(vault / "_meta" / "interests.toml")), 1)
            fresh = next(i for i in ledger if i.name == "Example Band")
            self.assertEqual(fresh.status, "active")


    def test_evidence_lists_pending_and_resolve_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = build_vault(Path(tmp))
            self._run(vault, "ingest", "--source", "experience-log")
            proc = self._run(vault, "evidence", "--days", "10", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual([p["title"] for p in data["pending"]], ["Example Team vs Other"])
            self.assertTrue(any("Example Singer" in c["text"] for c in data["diary_candidates"]))
            self.assertIn("Example Band", data["known"])
            pid = data["pending"][0]["id"]
            # The orchestrator judged it and recorded the event itself.
            self._run(vault, "add", "--name", "Example Star", "--kind", "player", "--event", "watched", "--date", "2098-11-05")
            self.assertIn("resolved", self._run(vault, "resolve", pid).stdout)
            self.assertEqual(json.loads(self._run(vault, "evidence", "--json").stdout)["pending"], [])
            self.assertEqual(self._run(vault, "resolve", pid).returncode, 1)


class ActNameTests(unittest.TestCase):
    def test_event_titles_reduce_to_the_act(self):
        self.assertEqual(ix.act_name("Example Band: Endless Tour"), "Example Band")
        self.assertEqual(ix.act_name("Example Idol 个人演唱会"), "Example Idol")
        self.assertEqual(ix.act_name("Example Idol 的世界巡回演唱会 2099"), "Example Idol")
        self.assertEqual(ix.act_name("Example Duo - Live in Example City"), "Example Duo")
        self.assertEqual(ix.act_name("Plain Name"), "Plain Name")
