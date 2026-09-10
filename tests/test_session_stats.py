"""session_stats must read bucketed session logs and tolerate schema drift."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class SessionStatsTest(unittest.TestCase):
    def test_counts_types_and_fill_rates_across_buckets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-stats-") as tmp:
            vault = Path(tmp) / "vault"
            flat = vault / "sessions"
            bucket = vault / "sessions" / "2099-01"
            bucket.mkdir(parents=True)
            shared = "## Continuity\ncarry\n\n## Anomalies\n- (none)\n"
            compact = shared + "\n## Operations\n- Outcomes: complete\n"
            full = "## Search Log\n| query | tool | 1 | result | yes |\n\n" + shared
            for name, body in (
                ("2099-01-05-reflection.md", compact),
                ("2099-01-06-reading.md", full),
                ("2099-01-07-reflection-2.md", compact),
            ):
                (bucket / name).write_text(body, encoding="utf-8")
            (flat / "2099-01-08-decision.md").write_text(compact, encoding="utf-8")
            (flat / "2099-02-30-reflection.md").write_text(shared, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "scripts/session_stats.py", "--window-days", "0", "--json"],
                cwd=REPO_ROOT,
                env={**os.environ, "OV": str(vault)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["session_logs"], 4)
            self.assertEqual(stats["per_type"], {"decision": 1, "reading": 1, "reflection": 2})
            self.assertEqual(stats["fill_rate"]["Continuity"], 1.0)
            self.assertEqual(stats["fill_rate"]["Anomalies"], 0.0)
            self.assertEqual(stats["sections"]["Operations"], {"present": 3, "filled": 3})

    def test_generated_empty_skeletons_do_not_count_as_filled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-stats-skeleton-") as tmp:
            env = {**os.environ, "OV": tmp}
            for _ in range(3):
                proc = subprocess.run([sys.executable, "scripts/session_log.py", "--type", "prm"],
                    cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            proc = subprocess.run([sys.executable, "scripts/session_stats.py", "--window-days", "0", "--json"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["session_logs"], 3)
            self.assertTrue(stats["fill_rate"])
            self.assertEqual(set(stats["fill_rate"].values()), {0.0})


if __name__ == "__main__":
    unittest.main()
