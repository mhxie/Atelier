"""`intent-misses --propose --json` must carry the same proposals as text mode.

Why this exists: the /triage overview reads this probe as JSON. Proposals used
to print only in text mode, so the JSON lane was always clear while the text
mode had rows to adopt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _event(raw: str, clarified: str | None = None) -> str:
    payload = {"raw_input": raw, "match_kind": "fallback", "timestamp": "2099-01-01T09:00:00"}
    if clarified:
        payload["clarified_to"] = clarified
    return json.dumps(payload, ensure_ascii=False)


class ProposalJsonTest(unittest.TestCase):
    def _run(self, vault: Path, *argv: str) -> dict:
        proc = subprocess.run(
            [sys.executable, "scripts/intent_coverage.py", "intent-misses", *argv],
            cwd=REPO_ROOT,
            env={**os.environ, "OV": str(vault)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_repeaters_appear_under_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            misses = vault / "_meta" / "intent_misses"
            misses.mkdir(parents=True)
            for day in ("2099-01-01", "2099-01-02", "2099-01-03"):
                (misses / f"{day}.jsonl").write_text(
                    _event("plan my week", "weekly") + "\n", encoding="utf-8"
                )
            # Seen often, but on one day only: not a proposal.
            with (misses / "2099-01-03.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(_event("one-off") + "\n" + _event("one-off") + "\n")
            payload = self._run(vault, "--propose", "--json")
            self.assertEqual(
                payload["proposals"],
                [{"phrase": "plan my week", "target": "weekly", "count": 3, "distinct_days": 3}],
            )
            self.assertEqual(payload["proposal_threshold_days"], 3)
            # Without --propose the key is present but null, so a reader can
            # tell "not asked" from "nothing to propose".
            self.assertIsNone(self._run(vault, "--json")["proposals"])


if __name__ == "__main__":
    unittest.main()
