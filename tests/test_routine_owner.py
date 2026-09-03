"""routine_owner: running claims block a transfer unless declared stale."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import routine_owner as ro  # noqa: E402


class RunningClaimsTest(unittest.TestCase):
    def test_stale_running_claims_can_be_ignored_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "_meta" / "routine_runs" / "demo"
            runs.mkdir(parents=True)
            fresh = runs / "2099-01-02.toml"
            fresh.write_text('status = "running"\n', encoding="utf-8")
            stuck = runs / "2099-01-01.toml"
            stuck.write_text('status = "running"\n', encoding="utf-8")
            old = time.time() - 30 * 3600
            os.utime(stuck, (old, old))
            done = runs / "2098-12-31.toml"
            done.write_text('status = "completed"\n', encoding="utf-8")
            saved = os.environ.get("OV")
            try:
                os.environ["OV"] = tmp
                self.assertEqual(sorted(p.name for p in ro._active_running_claims()), ["2099-01-01.toml", "2099-01-02.toml"])
                self.assertEqual([p.name for p in ro._active_running_claims(24)], ["2099-01-02.toml"])
                self.assertEqual(ro._active_running_claims(0.0), [])
            finally:
                if saved is not None:
                    os.environ["OV"] = saved
                else:
                    os.environ.pop("OV", None)


if __name__ == "__main__":
    unittest.main()
