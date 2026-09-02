"""Regression tests for the deterministic daily-brief tracking refresher."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_tracking as rt  # noqa: E402

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2099, 1, 31, 8, 0, tzinfo=ZONE)


def build_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "_meta").mkdir(parents=True)
    (vault / "cache").mkdir()
    (vault / "_meta" / "anilist.toml").write_text(
        """[anime]
username = "fixture"
timezone = "America/Los_Angeles"

[[followup]]
media_id = 99
""",
        encoding="utf-8",
    )
    (vault / "_meta" / "concerts.toml").write_text(
        """[concerts]
remind_days = 14
items = []
""",
        encoding="utf-8",
    )
    return vault


class FakeAniList:
    def __init__(self) -> None:
        self.schedule_variables: dict[str, object] | None = None

    def __call__(self, query: str, variables: dict[str, object]) -> dict:
        if "MediaListCollection" in query:
            return {
                "MediaListCollection": {
                    "lists": [
                        {
                            "entries": [
                                {
                                    "status": "CURRENT",
                                    "progress": 3,
                                    "media": {
                                        "id": 10,
                                        "title": {"userPreferred": "Current Show"},
                                    },
                                },
                                {
                                    "status": "COMPLETED",
                                    "progress": 12,
                                    "media": {
                                        "id": 20,
                                        "title": {"english": "Old Show"},
                                    },
                                },
                            ]
                        }
                    ]
                }
            }
        if "airingSchedules" in query:
            self.schedule_variables = variables
            airing = int(datetime(2099, 1, 31, 7, 30, tzinfo=ZONE).timestamp())
            return {
                "Page": {
                    "airingSchedules": [
                        {
                            "episode": 4,
                            "airingAt": airing,
                            "mediaId": 10,
                            "media": {"title": {"userPreferred": "Current Show"}},
                        }
                    ]
                }
            }
        if "media(id_in" in query:
            return {
                "Page": {
                    "media": [
                        {
                            "id": 99,
                            "status": "RELEASING",
                            "startDate": {"year": 2099, "month": 4, "day": 2},
                            "title": {"userPreferred": "Followed Sequel"},
                        }
                    ]
                }
            }
        raise AssertionError(f"unexpected query: {query}")


class TrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = build_vault(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_exact_same_day_schedule_for_current_entries(self):
        fake = FakeAniList()
        section = rt._anime_updates(self.vault, NOW, fake)
        self.assertEqual(len(section["library"]), 2)
        self.assertEqual(section["updates"], ["Current Show Ep.4 07:30 PST 已更新"])
        self.assertEqual(fake.schedule_variables["ids"], [10])

    def test_empty_schedule_does_not_infer_last_week(self):
        fake = FakeAniList()

        def query(query_text: str, variables: dict[str, object]) -> dict:
            if "airingSchedules" in query_text:
                return {"Page": {"airingSchedules": []}}
            return fake(query_text, variables)

        section = rt._anime_updates(self.vault, NOW, query)
        self.assertEqual(section["updates"], [])

    def test_followup_change_surfaces_and_survives_same_day_rerun(self):
        fake = FakeAniList()
        previous = {
            "date": NOW.date().isoformat(),
            "items": [
                {
                    "id": 99,
                    "title": "Followed Sequel",
                    "status": "NOT_YET_RELEASED",
                    "start_date": None,
                }
            ],
            "updates": [],
        }
        first = rt._followup_updates(self.vault, NOW, previous, fake)
        self.assertEqual(
            first["updates"], ["Followed Sequel 档期更新：未定 → 2099-04-02"]
        )
        second = rt._followup_updates(self.vault, NOW, first, fake)
        self.assertEqual(second["updates"], first["updates"])

    def test_refresh_preserves_unknown_keys_and_last_success_on_api_failure(self):
        cache_path = self.vault / "cache" / rt.CACHE_NAME
        cache_path.write_text(
            json.dumps(
                {
                    "refreshed_at": "2099-01-29T05:30:00-08:00",
                    "anime": {
                        "date": "2099-01-29",
                        "last_success_at": "2099-01-29T05:30:00-08:00",
                        "updates": ["preserve me"],
                    },
                    "followups": {"date": "2099-01-29", "items": [], "updates": []},
                    "acg": {"owned_elsewhere": True},
                }
            ),
            encoding="utf-8",
        )

        def unavailable(_query: str, _variables: dict[str, object]) -> dict:
            raise URLError("offline")

        result = rt.refresh(self.vault, NOW, query_fn=unavailable)
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["anime"]["updates"], ["preserve me"])
        self.assertIn("failed_at", saved["anime"])
        self.assertEqual(saved["acg"], {"owned_elsewhere": True})
        self.assertEqual(result["successes"], ["concerts"])
        self.assertEqual(len(result["errors"]), 2)


if __name__ == "__main__":
    unittest.main()
