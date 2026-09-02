#!/usr/bin/env python3
"""Fail closed before a private routine prompt is sent to a headless model."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SECRET_PATTERNS = (
    re.compile(
        r"(?i)authorization\s*:\s*(?:basic|token|bearer)\s+"
        r"(?!<|\$\{|\{\{|redacted\b|placeholder\b)[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(
        r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
        r"secret[_-]?access[_-]?key|password|passwd|secret)[\"']?\s*[:=]\s*[\"']?"
        r"(?!<|\$\{|\{\{|redacted\b|placeholder\b|example\b|changeme\b)"
        r"[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(
        r"(?m)(?:^|\s)(?:[A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD)|"
        r"AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY)\s*=\s*[\"']?"
        r"(?!<|\$\{|\{\{|REDACTED\b|PLACEHOLDER\b|EXAMPLE\b|CHANGEME\b)"
        r"[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(
        r"(?i)\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|"
        r"AIza[A-Za-z0-9_-]{20,})\b"
    ),
    re.compile(
        r"(?i)--(?:api[_-]?)?token(?:=|\s+)[\"']?"
        r"(?!<|\$\{|\{\{|redacted\b|placeholder\b)[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]{8,}@"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
ORIGINAL_PROMPT_MARKER = re.compile(
    r"(?m)^--- ORIGINAL ROUTINE PROMPT .* ---\s*$"
)
# Archived bodies were authored against a Drive MCP that supplied input and
# output. The local adapter overrides output only, so a body that still reads
# input from Drive needs the override to name a local input path.
DRIVE_MENTION = re.compile(
    r"(?i)\bgoogle[-\s]?drive\b|\bg?drive\s+mcp\b|\bmy\s+drive\b|\bgdrive\b"
)
# Two independent tokens searched across the whole override, not one ordered
# line: the directive is a long sentence, and a line-anchored match would fail
# the entire fleet closed on a single editor reflow.
LOCAL_INPUT_LOCATION = re.compile(r"(?i)\blocal\s+(?:file\s*system|filesystem|disk)\b")
LOCAL_INPUT_ROOT = re.compile(r"\$\{?OV\}?")


def check(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8")
    lines: set[int] = set()
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            lines.add(text.count("\n", 0, match.start()) + 1)
    return sorted(lines)


def structure_error(path: Path, *, require_local_input: bool = False) -> str | None:
    """Validate the local-adapter preamble.

    `require_local_input` is off by default because the cloud bundle and the
    registry audit share this check, and a cloud run reads from Drive
    correctly. Only the local `/run-routine` path enables it.
    """
    text = path.read_text(encoding="utf-8")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if not first_line.startswith("LOCAL EXECUTION OVERRIDE"):
        return "first line must begin with LOCAL EXECUTION OVERRIDE"
    marker = ORIGINAL_PROMPT_MARKER.search(text)
    if marker is None:
        return "missing ORIGINAL ROUTINE PROMPT boundary marker"
    override, body = text[: marker.start()], text[marker.end() :]
    if require_local_input and DRIVE_MENTION.search(body) and not (
        LOCAL_INPUT_LOCATION.search(override) and LOCAL_INPUT_ROOT.search(override)
    ):
        return (
            "archived body references Google Drive but the override declares "
            "no local filesystem input path rooted at $OV"
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject archived prompts with literal credentials.")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"ERROR: routine prompt missing: {args.path}", file=sys.stderr)
        return 2
    try:
        invalid_structure = structure_error(args.path, require_local_input=True)
        findings = check(args.path)
    except OSError as exc:
        print(f"ERROR: cannot read routine prompt: {exc}", file=sys.stderr)
        return 2
    if invalid_structure:
        print(f"ERROR: invalid local-adapter preamble: {invalid_structure}", file=sys.stderr)
        return 1
    if findings:
        joined = ", ".join(str(line) for line in findings)
        print(
            f"ERROR: literal credential detected in routine prompt at line(s): {joined}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
