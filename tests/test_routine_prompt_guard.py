"""routine_prompt_guard: the credential screen and local-adapter preamble check."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import routine_prompt_guard as guard  # noqa: E402

GOOD = textwrap.dedent(
    """\
    LOCAL EXECUTION OVERRIDE: inputs come from the local filesystem rooted at $OV; output is written under $OV.

    --- ORIGINAL ROUTINE PROMPT (demo) ---

    Read the notes from Google Drive and summarize them.
    """
)


class PromptGuardTest(unittest.TestCase):
    def _write(self, tmp: str, text: str) -> Path:
        path = Path(tmp) / "prompt.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_local_adapter_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, GOOD)
            self.assertIsNone(guard.structure_error(path, require_local_input=True))
            self.assertEqual(guard.check(path), [])

    def test_missing_override_or_marker_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            no_first = self._write(tmp, GOOD.replace("LOCAL EXECUTION OVERRIDE", "OVERRIDE"))
            self.assertIn("LOCAL EXECUTION OVERRIDE", guard.structure_error(no_first) or "")
            no_marker = self._write(tmp, GOOD.replace("--- ORIGINAL ROUTINE PROMPT (demo) ---", "--- prompt ---"))
            self.assertIn("boundary marker", guard.structure_error(no_marker) or "")

    def test_drive_body_needs_a_local_input_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vague = self._write(tmp, GOOD.replace("from the local filesystem rooted at $OV", "locally"))
            self.assertIsNone(guard.structure_error(vague))  # cloud bundle path does not require it
            self.assertIn("Google Drive", guard.structure_error(vague, require_local_input=True) or "")

    def test_literal_credentials_are_flagged_by_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            leaky = self._write(tmp, GOOD + "\nUse token sk-" + "a" * 24 + " for the API.\n")
            lines = guard.check(leaky)
            self.assertEqual(len(lines), 1)
            self.assertEqual(leaky.read_text(encoding="utf-8").splitlines()[lines[0] - 1][:9], "Use token")

    def test_cli_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = self._write(tmp, GOOD)
            proc = subprocess.run([sys.executable, "scripts/routine_prompt_guard.py", str(good)], cwd=REPO_ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            bad = Path(tmp) / "bad.md"
            bad.write_text(GOOD + "\nghp_" + "b" * 30 + "\n", encoding="utf-8")
            proc = subprocess.run([sys.executable, "scripts/routine_prompt_guard.py", str(bad)], cwd=REPO_ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("literal credential", proc.stderr)
            self.assertNotIn("ghp_", proc.stderr, "the guard must not echo the credential")
            proc = subprocess.run([sys.executable, "scripts/routine_prompt_guard.py", str(Path(tmp) / "missing.md")], cwd=REPO_ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
