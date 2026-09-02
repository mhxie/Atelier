"""Mutation tests for the runtime-fallback judgments in routine_fallback.py.

Pinned shapes: a timeout must never fall back (budget spent, host state
shared), the runner's own preflight code must never fall back (no model ran),
an unrecognised nonzero exit must still fall back (eligibility is by exit
code, signatures only label), and the Claude envelope extractor must fail
closed on an error envelope rather than hand a partial object to attestation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import routine_fallback as rf  # noqa: E402


class DecideTest(unittest.TestCase):
    def test_no_fallback_runtime_never_falls_back(self) -> None:
        verdict = rf.decide(1, "usage limit reached", None)
        self.assertFalse(verdict["fallback"])
        self.assertEqual(verdict["reason"], "no-fallback-runtime")

    def test_unsupported_runtime_is_refused(self) -> None:
        verdict = rf.decide(1, "", "gemini")
        self.assertFalse(verdict["fallback"])
        self.assertTrue(verdict["reason"].startswith("unsupported-fallback-runtime"))

    def test_timeout_never_falls_back(self) -> None:
        verdict = rf.decide(rf.TIMEOUT_EXIT, "usage limit reached", "claude")
        self.assertFalse(verdict["fallback"])
        self.assertEqual(verdict["reason"], "timeout-budget-spent")

    def test_runner_preflight_never_falls_back(self) -> None:
        verdict = rf.decide(rf.PREFLIGHT_EXIT, "ERROR: invalid local-adapter preamble", "claude")
        self.assertFalse(verdict["fallback"])
        self.assertEqual(verdict["reason"], "runner-preflight-failed")

    def test_success_never_falls_back(self) -> None:
        self.assertFalse(rf.decide(0, "", "claude")["fallback"])

    def test_usage_limit_is_labeled(self) -> None:
        log = "some work\nERROR: You've hit your usage limit. Try again later.\n"
        verdict = rf.decide(1, log, "claude")
        self.assertTrue(verdict["fallback"])
        self.assertEqual(verdict["reason"], "usage-limit")
        self.assertEqual(verdict["runtime"], "claude")

    def test_rate_limit_429_is_usage_limit(self) -> None:
        verdict = rf.decide(1, "HTTP 429 Too Many Requests", "claude")
        self.assertEqual(verdict["reason"], "usage-limit")

    def test_bare_numbers_in_transcripts_do_not_label(self) -> None:
        # Routine transcripts quote figures; "$19,045,429,872K" is not a 429.
        verdict = rf.decide(1, "AMAT held by 43 funds, $19,045,429,872K total\nstep 503 done", "claude")
        self.assertEqual(verdict["reason"], "codex-exit-1")

    def test_auth_is_labeled(self) -> None:
        verdict = rf.decide(1, "error: not logged in, run codex login", "claude")
        self.assertEqual(verdict["reason"], "auth")

    def test_unrecognised_nonzero_exit_still_falls_back(self) -> None:
        verdict = rf.decide(3, "panic: something unexpected", "claude")
        self.assertTrue(verdict["fallback"])
        self.assertEqual(verdict["reason"], "codex-exit-3")

    def test_codex_not_found_falls_back(self) -> None:
        verdict = rf.decide(rf.NOT_FOUND_EXIT, "", "claude")
        self.assertTrue(verdict["fallback"])
        self.assertEqual(verdict["reason"], "codex-not-found")

    def test_signature_only_scans_the_tail(self) -> None:
        # A quota mention 500 lines up is stale context, not the failure.
        log = "usage limit\n" + "\n".join(f"row {i}" for i in range(500)) + "\nboom\n"
        verdict = rf.decide(1, log, "claude")
        self.assertEqual(verdict["reason"], "codex-exit-1")


class ExtractTest(unittest.TestCase):
    def test_structured_output_wins(self) -> None:
        envelope = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "prose summary",
                "structured_output": {"routine": "x", "outcome": "delivered"},
            }
        )
        self.assertEqual(
            rf.extract_claude_result(envelope), {"routine": "x", "outcome": "delivered"}
        )

    def test_result_string_json_is_accepted(self) -> None:
        envelope = json.dumps({"type": "result", "result": json.dumps({"routine": "x"})})
        self.assertEqual(rf.extract_claude_result(envelope), {"routine": "x"})

    def test_error_envelope_fails_closed(self) -> None:
        envelope = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "Usage credits required",
                "structured_output": {"routine": "x", "outcome": "delivered"},
            }
        )
        with self.assertRaises(ValueError):
            rf.extract_claude_result(envelope)

    def test_prose_only_result_is_rejected(self) -> None:
        envelope = json.dumps({"type": "result", "result": "I wrote the file."})
        with self.assertRaises(ValueError):
            rf.extract_claude_result(envelope)

    def test_non_json_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rf.extract_claude_result("not json")


class CliTest(unittest.TestCase):
    def test_decide_cli_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "model.log"
            log.write_text("rate limit exceeded\n", encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "routine_fallback.py"),
                    "decide",
                    "--exit-code",
                    "1",
                    "--log",
                    str(log),
                    "--fallback-runtime",
                    "claude",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            verdict = json.loads(proc.stdout)
            self.assertTrue(verdict["fallback"])
            self.assertEqual(verdict["reason"], "usage-limit")

    def test_decide_cli_treats_none_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "model.log"
            log.write_text("", encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "routine_fallback.py"),
                    "decide",
                    "--exit-code",
                    "1",
                    "--log",
                    str(log),
                    "--fallback-runtime",
                    "none",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertFalse(json.loads(proc.stdout)["fallback"])

    def test_extract_cli_writes_result_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = Path(tmp) / "envelope.json"
            out = Path(tmp) / "result.json"
            envelope.write_text(
                json.dumps({"structured_output": {"routine": "x", "outcome": "noop"}}),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "routine_fallback.py"),
                    "extract",
                    "--envelope",
                    str(envelope),
                    "--out",
                    str(out),
                ],
                check=True,
            )
            self.assertEqual(json.loads(out.read_text()), {"routine": "x", "outcome": "noop"})


if __name__ == "__main__":
    unittest.main()
