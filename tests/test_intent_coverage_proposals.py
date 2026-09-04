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


def _event(raw: str, clarified: str | None = None, kind: str = "general") -> str:
    payload = {"raw_input": raw, "match_kind": kind, "timestamp": "2099-01-01T09:00:00"}
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
            misses = vault / "_meta" / "intent_routes"
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

    def test_retired_router_miss_log_is_not_read(self) -> None:
        """`_meta/intent_misses/` (miss-only, hit-less by construction) must not
        enter the ledger: it made route coverage 0.0 and the cue claim "no hits"."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            routes = vault / "_meta" / "intent_routes"
            routes.mkdir(parents=True)
            (routes / "2099-01-01.jsonl").write_text(
                _event("plan my week", kind="routed") + "\n", encoding="utf-8"
            )
            legacy = vault / "_meta" / "intent_misses"
            legacy.mkdir()
            for day in ("2099-01-01", "2099-01-02", "2099-01-03"):
                (legacy / f"{day}.jsonl").write_text(
                    _event("old miss", kind="fallback") + "\n", encoding="utf-8"
                )
            payload = self._run(vault, "--propose", "--json")
            self.assertEqual(payload["total_events"], 1, payload)
            self.assertEqual(payload["routed_events"], 1, payload)
            self.assertEqual(payload["proposals"], [], payload)
            self.assertEqual(payload["log_dir"], str(routes))
            # The eval metric reads the default ledger location; it must see
            # the same single event, not the three retired-router lines.
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json, sys\nsys.path.insert(0, 'scripts')\nimport eval_run\n"
                    "from datetime import date\n"
                    "print(json.dumps(eval_run.eval_routing(today=date(2099, 1, 2))))",
                ],
                cwd=REPO_ROOT,
                env={**os.environ, "OV": str(vault)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            routing = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(routing["cases"], 1, routing)
            self.assertEqual(routing["score"], 1.0, routing)


if __name__ == "__main__":
    unittest.main()
