"""decay_scan low-signal band: all five conjunctive conditions, no model."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import decay_scan

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(vault: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/decay_scan.py"],
        cwd=REPO_ROOT, env={**os.environ, "OV": str(vault)},
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class LowSignalBandTest(unittest.TestCase):
    def test_five_conditions_are_conjunctive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atelier-decay-") as tmp:
            vault = Path(tmp) / "vault"
            wip = vault / "wip"
            (wip / "bucket").mkdir(parents=True)
            old = ("stale words " * 10) + "\n"
            hits = {
                "orphan.md": old,                      # fires: short, old, untagged, unlinked
                "bucket/nested-orphan.md": old,        # fires inside a fission bucket too
            }
            misses = {
                "tagged.md": old + "#keep\n",          # has a tag
                "linked.md": old,                      # will be wikilinked below
                "long.md": ("word " * 200) + "\n",     # not short
                "fresh.md": old,                       # will stay recent
            }
            for name, body in {**hits, **misses}.items():
                (wip / name).write_text(body, encoding="utf-8")
            (vault / "research").mkdir()
            (vault / "research" / "refs.md").write_text("see [[linked]]\n", encoding="utf-8")
            ancient = 1_600_000_000  # 2020: safely past the 90-day floor
            for name in (*hits, "tagged.md", "linked.md", "long.md"):
                os.utime(wip / name, (ancient, ancient))
            out = _run(vault)
            fired = sorted(f["path"] for f in out["low_signal"])
            self.assertEqual(fired, ["wip/bucket/nested-orphan.md", "wip/orphan.md"])
            first = out["low_signal"][0]
            for key in ("words", "links_in", "tags", "age_days"):
                self.assertIn(key, first)
            self.assertFalse(out["redundant_ran"])


class QmdCandidateTest(unittest.TestCase):
    def test_scores_rank_candidates_without_legacy_floor(self):
        with tempfile.TemporaryDirectory(prefix="atelier-qmd-decay-") as tmp:
            vault = Path(tmp)
            base = vault / "wip"
            base.mkdir()
            (base / "candidate.md").write_text("candidate")
            rows = [{"path": path, "score": .01, "backend": "qmd"} for path in
                    ("wip/candidate.md", "wip/a.md", "research/b.md", "reflections/c.md")]
            native = subprocess.CompletedProcess([], 0, json.dumps(rows), "")
            with patch.object(decay_scan, "tier", return_value=base), \
                 patch.object(decay_scan, "_tier_of", side_effect=lambda path: path.split("/")[0]), \
                 patch.object(decay_scan.subprocess, "run", return_value=native):
                findings = decay_scan.scan_redundant(vault, "wip", 15)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["mode"], "qmd")
                self.assertTrue(findings[0]["requires_content_review"])
                native.stdout = json.dumps([rows[1], rows[1], rows[2]])
                self.assertEqual(decay_scan.scan_redundant(vault, "wip", 15), [])
                native.returncode = 2
                with self.assertRaisesRegex(RuntimeError, "retrieval failed"):
                    decay_scan.scan_redundant(vault, "wip", 15)


if __name__ == "__main__":
    unittest.main()
