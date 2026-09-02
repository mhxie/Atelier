#!/usr/bin/env python3
"""Deterministic halves of the local-routine runtime fallback.

`routine_runner.sh` runs unattended routines through Codex. When Codex fails
before delivering an artifact, a profile may declare `fallback_runtime` and
the runner re-executes the same cycle through that runtime. Two judgments
live here rather than in bash so they can be unit-tested:

  decide      whether a primary-runtime failure is fallback-eligible, and a
              labeled reason for the claim
  extract     copy the structured result out of a `claude -p --output-format
              json` envelope into the plain result file that
              `routine_result.py` attests

A timeout (exit 124) is never fallback-eligible: the profile budget is spent
and the host state that produced it (sleep, wedged network) would hit the
second runtime too. Exit 2 is the runner's own preflight code, raised before
any model launched, so retrying a different model cannot help.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TIMEOUT_EXIT = 124
PREFLIGHT_EXIT = 2
NOT_FOUND_EXIT = 127
FALLBACK_RUNTIMES = {"claude"}

# Ordered: the first match labels the reason. Labels only; eligibility is
# decided by exit code so an unrecognised failure still falls back.
SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "usage-limit",
        re.compile(
            r"usage[ _-]?limit|rate[ _-]?limit|too many requests|"
            r"(?:status|http|error|code)\D{0,12}\b429\b|"
            r"insufficient_quota|quota exceeded|out of (?:tokens|credits)|credit balance",
            re.IGNORECASE,
        ),
    ),
    (
        "auth",
        re.compile(
            r"not logged in|please (?:log|sign) in|unauthori[sz]ed|(?:status|http|error|code)\D{0,12}\b401\b|"
            r"invalid[_ ]api[_ ]key|authentication",
            re.IGNORECASE,
        ),
    ),
    (
        "upstream",
        re.compile(
            r"(?:status|http|error|code)\D{0,12}\b5\d\d\b|server error|bad gateway|service unavailable|"
            r"connection (?:reset|refused|error)|network (?:error|unreachable)",
            re.IGNORECASE,
        ),
    ),
)


def decide(exit_code: int, log_text: str, fallback_runtime: str | None) -> dict:
    """Return {"fallback": bool, "reason": str, "runtime": str | None}."""
    if not fallback_runtime:
        return {"fallback": False, "reason": "no-fallback-runtime", "runtime": None}
    if fallback_runtime not in FALLBACK_RUNTIMES:
        return {
            "fallback": False,
            "reason": f"unsupported-fallback-runtime:{fallback_runtime}",
            "runtime": None,
        }
    if exit_code == 0:
        return {"fallback": False, "reason": "primary-succeeded", "runtime": None}
    if exit_code == TIMEOUT_EXIT:
        return {"fallback": False, "reason": "timeout-budget-spent", "runtime": None}
    if exit_code == PREFLIGHT_EXIT:
        return {"fallback": False, "reason": "runner-preflight-failed", "runtime": None}
    if exit_code == NOT_FOUND_EXIT:
        return {"fallback": True, "reason": "codex-not-found", "runtime": fallback_runtime}
    tail = "\n".join(log_text.splitlines()[-200:])
    for label, pattern in SIGNATURES:
        if pattern.search(tail):
            return {"fallback": True, "reason": label, "runtime": fallback_runtime}
    return {
        "fallback": True,
        "reason": f"codex-exit-{exit_code}",
        "runtime": fallback_runtime,
    }


def extract_claude_result(envelope_text: str) -> dict:
    """Return the schema-shaped result object from a `claude -p` JSON envelope.

    `--output-format json` prints one object whose `structured_output` field
    carries the validated object. Older builds put only the JSON text in
    `result`; accept that too. Anything else is a failed delivery.
    """
    try:
        envelope = json.loads(envelope_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"claude envelope is not JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError("claude envelope must be a JSON object")
    if envelope.get("is_error") is True:
        detail = envelope.get("result") or envelope.get("subtype") or "unknown"
        raise ValueError(f"claude reported an error result: {str(detail)[:300]}")
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = envelope.get("result")
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("claude envelope carries no structured result object")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    decide_parser = sub.add_parser("decide", help="judge a primary-runtime failure")
    decide_parser.add_argument("--exit-code", type=int, required=True)
    decide_parser.add_argument("--log", type=Path, required=True)
    decide_parser.add_argument("--fallback-runtime", default="")

    extract_parser = sub.add_parser(
        "extract", help="write the structured object out of a claude JSON envelope"
    )
    extract_parser.add_argument("--envelope", type=Path, required=True)
    extract_parser.add_argument("--out", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "decide":
        try:
            log_text = args.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""
        runtime = args.fallback_runtime.strip() or None
        if runtime == "none":
            runtime = None
        print(json.dumps(decide(args.exit_code, log_text, runtime)))
        return 0
    try:
        result = extract_claude_result(args.envelope.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
