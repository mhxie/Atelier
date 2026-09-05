"""Contract guard for the Reviewer's system modes.

Why this exists: on 2026-09-03 a System Diff Review dispatched with a plain
"do not modify" instruction reverted two scripts under review and began
re-applying the diff hunk by hunk; the working tree had to be restored from
backups and both reviews re-run. reviewer.md granted Bash and said nothing
about writes. The rule now lives in the agent definition, and this test keeps
it there.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / ".claude" / "agents" / "reviewer.md"


class ReviewerReadOnlyContractTest(unittest.TestCase):
    def test_system_modes_declare_read_only_and_the_baseline_command(self) -> None:
        text = SPEC.read_text(encoding="utf-8")
        diff_mode = text.index("## System Diff Review Mode")
        rule = text.index("Both system modes are read-only", diff_mode)
        self.assertLess(rule - diff_mode, 400)
        window = text[rule : rule + 400]
        for verb in ("stash", "checkout", "restore", "apply", "reset", "edit"):
            self.assertIn(verb, window)
        self.assertIn("git show HEAD:<path>", window)
        self.assertIn("git show <base>:<path>", window)
        self.assertIn("has no baseline", window)


if __name__ == "__main__":
    unittest.main()
