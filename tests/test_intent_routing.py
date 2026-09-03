"""Catalog integrity: the rows `/hi` classifies against must be well-formed.

Routing is model judgment over `description`, so the deterministic guard is
that every row has one, the routing evalset only names real rows, and the
`catalog` subcommand renders a compact projection the orchestrator can read.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "routing_evalset.json"
INTENTS = REPO_ROOT / "harness" / "intents.toml"


class IntentCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.intents = tomllib.loads(INTENTS.read_text(encoding="utf-8"))["intents"]

    def test_every_row_has_a_one_line_description_and_no_retired_keys(self) -> None:
        for name, row in self.intents.items():
            with self.subTest(intent=name):
                description = row.get("description")
                self.assertIsInstance(description, str)
                self.assertTrue(description.strip())
                self.assertNotIn("\n", description.strip())
                self.assertNotIn("patterns", row)
                self.assertNotIn("priority", row)
                examples = row.get("examples", [])
                self.assertIsInstance(examples, list)
                self.assertTrue(all(isinstance(e, str) for e in examples))
        self.assertIn("general", self.intents)

    def test_descriptions_are_distinct(self) -> None:
        seen: dict[str, str] = {}
        for name, row in self.intents.items():
            key = row["description"].strip().casefold()
            self.assertNotIn(key, seen, f"{name} duplicates {seen.get(key)}")
            seen[key] = name

    def test_evalset_names_only_real_rows(self) -> None:
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        self.assertGreaterEqual(len(cases), 20)
        unknown = sorted({c["expected"] for c in cases} - set(self.intents))
        self.assertFalse(unknown, f"evalset expects rows that do not exist: {unknown}")

    def test_catalog_subcommand_renders_every_row(self) -> None:
        proc = subprocess.run(
            [sys.executable, "scripts/intent_coverage.py", "catalog", "--json"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)
        # The gitignored overlay may append private rows; the public catalog
        # must still come first, unchanged, and every extra row must say so.
        self.assertEqual([r["name"] for r in rows if not r["private"]], list(self.intents))
        for row in rows:
            if row["private"]:
                self.assertNotIn(row["name"], self.intents)
                self.assertTrue(row["description"].strip())
            else:
                self.assertEqual(row["description"], self.intents[row["name"]]["description"])
        text = subprocess.run(
            [sys.executable, "scripts/intent_coverage.py", "catalog"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        ).stdout
        self.assertEqual(len(text.strip().splitlines()), len(rows))
        self.assertLess(len(text.encode("utf-8")), 8192, "catalog must stay a small read")


if __name__ == "__main__":
    unittest.main()
