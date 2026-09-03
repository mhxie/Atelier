"""The decision ledger: reasons are mandatory, silence is never imported, vetoes count."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent import futures
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import decisions  # noqa: E402


def _run(ledger: Path, *argv: str, vault: Path | None = None) -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/decisions.py", "--ledger", str(ledger), *argv],
        cwd=REPO_ROOT, env={**os.environ, "OV": str(vault or ledger.parent)},
        capture_output=True, text=True, timeout=60,
    )
    payload = json.loads(proc.stdout)
    payload["_exit"] = proc.returncode
    return payload


QUEUE = """schema_version = 1

[[pending]]
id = "a"
category = "time-stale-A"
proposed_action = "close the lapsed plan"
evidence_summary = "by end of Q3 2025"
proposed_at = "2099-01-01"
status = "dismissed"
dismiss_reason = "plan is still active"
resolved_at = "2099-01-05"
peers = ["wip/plan.md"]

[[pending]]
id = "b"
category = "time-stale-A"
proposed_action = "verify the exercise"
evidence_summary = "before April"
proposed_at = "2099-01-01"
status = "auto-dismissed"
dismiss_reason = "older than 30d"
peers = ["wip/x.md"]

[[pending]]
id = "c"
category = "low-signal"
proposed_action = "archive"
evidence_summary = "87 words"
proposed_at = "2099-01-02"
status = "applied"
peers = ["wip/y.md"]
"""


class LedgerTest(unittest.TestCase):
    def test_record_requires_reason_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            bad = _run(ledger, "record", "--class", "t/x", "--subject", "s", "--verdict", "dismiss", "--reason", " ")
            self.assertEqual(bad["_exit"], 2)
            self.assertIn("reason", bad["error"])
            ok = _run(ledger, "record", "--class", "t/x", "--subject", "s", "--verdict", "dismiss",
                      "--reason", "still active", "--feature", "tier=wip", "--source", "triage")
            self.assertEqual(ok["_exit"], 0)
            rows = _run(ledger, "list", "--class", "t/x")["rows"]
            self.assertEqual(rows[0]["features"], {"tier": "wip"})
            self.assertEqual(rows[0]["by"], "human")

    def test_import_autoevo_skips_silence_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "_meta").mkdir()
            queue = vault / "_meta" / "autoevo_pending.toml"
            queue.write_text(QUEUE, encoding="utf-8")
            ledger = vault / "_meta" / "decisions.jsonl"
            out = _run(ledger, "import-autoevo", "--queue", str(queue), vault=vault)
            self.assertEqual([r["id"] for r in out["imported"]], ["a", "c"])
            self.assertIn({"id": "b", "reason": "status auto-dismissed"}, out["skipped"])
            rows = _run(ledger, "list", vault=vault)["rows"]
            self.assertEqual({r["subject"]: r["verdict"] for r in rows}, {"a": "dismiss", "c": "apply"})
            self.assertEqual(rows[0]["features"]["tier"], "wip")
            self.assertTrue(rows[1]["reason"].startswith("(no reason"))
            again = _run(ledger, "import-autoevo", "--queue", str(queue), vault=vault)
            self.assertEqual(again["imported"], [])
            self.assertEqual(len(_run(ledger, "list", vault=vault)["rows"]), 2)

    def test_stats_count_vetoes_against_precedent_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            lines = [
                {"ts": "2099-01-01T00:00:00", "class": "autoevo/time-stale-A", "subject": "p1", "verdict": "dismiss", "reason": "precedent", "features": {}, "source": "nightly", "by": "precedent"},
                {"ts": "2099-01-03T00:00:00", "class": "autoevo/time-stale-A", "subject": "p1", "verdict": "apply", "reason": "no, do it", "features": {}, "source": "autoevo-review", "by": "human"},
                {"ts": "2099-01-01T00:00:00", "class": "autoevo/time-stale-A", "subject": "p2", "verdict": "dismiss", "reason": "precedent", "features": {}, "source": "nightly", "by": "precedent"},
                {"ts": "2099-02-01T00:00:00", "class": "autoevo/time-stale-A", "subject": "p3", "verdict": "dismiss", "reason": "precedent", "features": {}, "source": "nightly", "by": "precedent"},
            ]
            ledger.write_text("".join(json.dumps(row) + "\n" for row in lines), encoding="utf-8")
            stats = _run(ledger, "stats", "--today", "2099-02-05")["classes"]["autoevo/time-stale-A"]
            # p1 vetoed by a later human line. p2 aged out with nobody looking, so it
            # is unconfirmed, not judged: silence is not a verdict. p3 is still inside
            # its window. Accuracy is over observed outcomes only, so one veto out of
            # one judged default is 0.0, not 0.5.
            self.assertEqual((stats["precedent_total"], stats["precedent_judged"], stats["precedent_vetoed"]), (3, 1, 1))
            self.assertEqual(stats["precedent_unconfirmed"], 1)
            self.assertEqual(stats["precedent_accuracy"], 0.0)
            self.assertEqual(stats["human"], {"apply": 1})


class LedgerAppendIsAtomicTest(unittest.TestCase):
    """Concurrent writers must not tear a line.

    A ledger line carries `features` and routinely exceeds the 4096-byte limit
    below which O_APPEND alone is atomic, and the nightly's `set-default` run
    can overlap an interactive resolve. `load` drops an unparseable line, so a
    tear surfaces as a precedent that quietly stopped existing rather than as
    an error. `os.write` releases the GIL, so these threads race in the kernel.
    """

    WRITERS = 3
    PER_WRITER = 25
    OVERSIZED = "x" * 6000

    def _append(self, ledger: Path, tag: str) -> None:
        for i in range(self.PER_WRITER):
            decisions.record(
                cls="c", subject=f"{tag}-{i}", verdict="dismiss", reason=self.OVERSIZED,
                features={"blob": self.OVERSIZED}, source="t", by="human", path=ledger,
            )

    def test_oversized_concurrent_appends_all_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "d.jsonl"
            with futures.ThreadPoolExecutor(max_workers=self.WRITERS) as pool:
                for future in futures.as_completed(
                    pool.submit(self._append, ledger, tag) for tag in "abc"
                ):
                    future.result()

            lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), self.WRITERS * self.PER_WRITER)
            subjects = {json.loads(ln)["subject"] for ln in lines}  # raises if a write tore
            self.assertEqual(len(subjects), self.WRITERS * self.PER_WRITER)


if __name__ == "__main__":
    unittest.main()
