"""dine_rank owns the log-side facts the model used to recompute every run."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HEADER = "| Date | Restaurant | City | 类型 | ⭐ | 评分 | 再去 | 健康 | 人数 | 总额 | 人均 | Platform | Credit | 必点·备注 |"
SEP = "|" + "---|" * 14


def _row(d: str, name: str, rating: str, again: str) -> str:
    return f"| {d} | {name} | Example City | 中餐 | — | {rating} | {again} | — | 2 | $100 | $50 | — | — | — |"


class DineRankTest(unittest.TestCase):
    def test_aggregates_scores_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-dine-") as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            tracker = vault / "tracker.md"
            tracker.write_text(
                "\n".join([
                    HEADER, SEP,
                    _row("2099-01-01", "Great Place", "9", "Y"),
                    _row("2099-02-01", "Great Place", "8", "Y"),
                    _row("2099-04-20", "Great Place", "9", "Y"),
                    _row("2099-01-15", "Bad Place", "4", "N"),
                    _row("2099-05-28", "Recent Spot", "7", "Y"),
                ]) + "\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, "scripts/dine_rank.py", "--tracker", str(tracker), "--today", "2099-06-01"],
                cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            great = out["restaurants"]["Great Place"]
            self.assertEqual(great["visits"], 3)
            self.assertEqual(great["avg_rating_last3"], 8.67)
            self.assertEqual(great["log_score"], 7)  # avg>=8 (+5) and 再去 Y (+2)
            bad = out["restaurants"]["Bad Place"]
            self.assertEqual(bad["log_score"], -7)  # avg<=5 (-3), 再去 N (-5), rusty (+1)
            self.assertIn("Recent Spot", out["excluded"])
            self.assertNotIn("Great Place", out["excluded"])
            self.assertEqual(out["sourced_rows"], 5)

    def test_carries_the_last_decided_revisit_forward(self) -> None:
        # profile/diet.md "Capture tiers": 再去 goes unfilled once a restaurant is
        # settled, so a dashed newest row must not erase the earlier verdict.
        with tempfile.TemporaryDirectory(prefix="atelier-dine-") as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            tracker = vault / "tracker.md"
            tracker.write_text(
                "\n".join([
                    HEADER, SEP,
                    _row("2099-01-01", "Settled Favourite", "9", "Y"),
                    _row("2099-02-01", "Settled Favourite", "9", "Y"),
                    _row("2099-03-01", "Settled Favourite", "9", "—"),
                    _row("2099-01-01", "Settled Avoid", "4", "N"),
                    _row("2099-02-01", "Settled Avoid", "4", "N"),
                    _row("2099-03-01", "Settled Avoid", "4", "—"),
                ]) + "\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, "scripts/dine_rank.py", "--tracker", str(tracker), "--today", "2099-06-01"],
                cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            fav = out["restaurants"]["Settled Favourite"]
            self.assertEqual(fav["last_again"], "Y")
            self.assertIn("再去 Y: +2", fav["log_score_parts"])
            avoid = out["restaurants"]["Settled Avoid"]
            self.assertEqual(avoid["last_again"], "N")
            self.assertIn("再去 N: -5", avoid["log_score_parts"])


if __name__ == "__main__":
    unittest.main()
