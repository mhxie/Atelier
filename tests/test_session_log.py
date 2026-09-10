"""Regression tests for structured Atelier session logs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import session_stats


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "session_log.py"


class SessionLogTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="atelier-session-log-")
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / "vault"
        self.env = {**os.environ, "OV": str(self.vault)}

    def run_log(self, session_type: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--type", session_type, *extra],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def content(self, result: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.vault / "sessions" / Path(result.stdout.strip()).name
        self.assertTrue(path.is_file())
        return path.read_text(encoding="utf-8")

    def test_prm_is_a_supported_session_type(self) -> None:
        content = self.content(
            self.run_log("prm", "--duration", "1", "--model", "fixture")
        )
        self.assertIn("type: prm", content)
        self.assertIn("duration_estimate: 1", content)
        self.assertIn("model: fixture", content)
        self.assertIn("| Query | Tool | Hits | Top Result | Useful |", content)
        self.assertNotIn("## Operations", content)

    def test_selected_types_emit_the_compact_sections(self) -> None:
        types = (
            "reflection",
            "weekly",
            "review",
            "decision",
            "energy-audit",
            "exploration",
        )
        for session_type in types:
            with self.subTest(session_type=session_type):
                content = self.content(self.run_log(session_type))
                self.assertIn(f"type: {session_type}", content)
                headings = [line for line in content.splitlines() if line.startswith("## ")]
                self.assertEqual(
                    headings, ["## Continuity", "## Anomalies", "## Operations"]
                )
                self.assertFalse(any(session_stats._sections(content).values()))

    def test_remaining_types_keep_the_full_sections(self) -> None:
        for session_type in (
            "curate", "introspect", "meeting", "deep-dive"
        ):
            with self.subTest(session_type=session_type):
                content = self.content(self.run_log(session_type))
                self.assertIn("## Agents Dispatched", content)
                self.assertIn("## Harness Assumptions Exercised", content)
                self.assertNotIn("## Operations", content)

    def test_reading_refuses_an_incomplete_log(self) -> None:
        result = self.run_log("reading")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be created complete", result.stderr)
        self.assertFalse((self.vault / "sessions").exists())

    def test_collision_overflow_never_overwrites_an_existing_log(self) -> None:
        sessions = self.vault / "sessions"
        sessions.mkdir(parents=True)
        now = datetime.now()
        effective_date = (
            (now - timedelta(days=1)).date() if now.hour < 3 else now.date()
        )
        base_id = f"{effective_date.isoformat()}-prm"
        existing = sessions / f"{base_id}-99.md"
        for sequence in range(1, 100):
            suffix = "" if sequence == 1 else f"-{sequence}"
            sessions.joinpath(f"{base_id}{suffix}.md").write_text(
                f"sentinel-{sequence}", encoding="utf-8"
            )

        result = self.run_log("prm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(existing.read_text(encoding="utf-8"), "sentinel-99")
        self.assertTrue(sessions.joinpath(f"{base_id}-100.md").is_file())
        self.assertEqual(len(list(sessions.glob(f"{base_id}*.md"))), 100)


if __name__ == "__main__":
    unittest.main()
