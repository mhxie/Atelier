"""routine_lock: the local claim helpers that every coordination mode relies on."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import routine_lock as rl  # noqa: E402


class ClaimHelpersTest(unittest.TestCase):
    def test_cycle_id_defaults_to_today(self) -> None:
        self.assertEqual(rl._cycle_id("2099-01-01"), "2099-01-01")
        self.assertEqual(rl._cycle_id(None), date.today().isoformat())

    def test_claim_path_requires_ov_and_lives_under_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            saved = os.environ.get("OV")
            try:
                os.environ["OV"] = tmp
                path = rl._claim_path("demo", "2099-01-01")
                self.assertEqual(path, Path(tmp) / "_meta" / "routine_runs" / "demo" / "2099-01-01.toml")
                os.environ.pop("OV")
                with self.assertRaises(ValueError):
                    rl._claim_path("demo", "2099-01-01")
            finally:
                if saved is not None:
                    os.environ["OV"] = saved

    def test_claim_status_fails_closed_on_unknown_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claim.toml"
            self.assertIsNone(rl._claim_status(path))
            path.write_text('status = "running"\n', encoding="utf-8")
            self.assertEqual(rl._claim_status(path), "running")
            path.write_text("status = 3\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                rl._claim_status(path)
            path.write_text("[broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                rl._claim_status(path)

    def test_atomic_write_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "claim.toml"
            rl._atomic_write(target, 'status = "completed"\n')
            self.assertEqual(target.read_text(encoding="utf-8"), 'status = "completed"\n')
            self.assertEqual([p.name for p in target.parent.iterdir()], ["claim.toml"])


if __name__ == "__main__":
    unittest.main()
