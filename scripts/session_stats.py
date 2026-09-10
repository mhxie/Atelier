#!/usr/bin/env python3
"""Aggregate session-log telemetry for on-demand inspection.

This tolerant reader counts present sections without treating empty table
headers or continuity labels as completed telemetry.

Output (one JSON object):
  per_type      counts of session logs by type suffix (reflection, reading, ...)
  fill_rate     fraction of sections with content beyond the heading, per section
  sections      per-heading totals: files having it, files where it is filled
  window_days   the trailing window scanned (default 90; 0 = all)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier_files  # noqa: E402

HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
EMPTY_MARKERS = {"", "(none)", "- (none)", "n/a", "-"}


def _sections(text: str) -> dict[str, bool]:
    """Map heading -> has substantive content before the next heading."""
    out: dict[str, bool] = {}
    matches = list(HEADING_RE.finditer(text))
    for i, m in enumerate(matches):
        body = text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        body = re.sub(r"(?m)^[ \t]*\|[^\n]*\|\r?\n[ \t]*\|(?:[ \t]*:?-+:?[ \t]*\|)+[ \t]*$", "", body)
        lines = [ln.strip() for ln in body.splitlines()]
        filled = any(ln.lower() not in EMPTY_MARKERS and not re.fullmatch(
            r"- [^:]+:\s*(?:none|\(none\)|n/a)?", ln, re.IGNORECASE) for ln in lines)
        out[m.group(1)] = out.get(m.group(1), False) or filled
    return out


def collect(window_days: int) -> dict:
    cutoff = date.today() - timedelta(days=window_days) if window_days else None
    per_type: dict[str, int] = defaultdict(int)
    have: dict[str, int] = defaultdict(int)
    filled: dict[str, int] = defaultdict(int)
    scanned = 0
    for path in tier_files("sessions", "*.md"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})-([a-z-]+?)(?:-\d+)?\.md$", path.name)
        if not m:
            continue
        try:
            day = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if cutoff and day < cutoff:
            continue
        scanned += 1
        per_type[m.group(2)] += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for heading, is_filled in _sections(text).items():
            have[heading] += 1
            if is_filled:
                filled[heading] += 1
    fill_rate = {
        h: round(filled[h] / have[h], 2) for h in sorted(have) if have[h] >= 3
    }
    return {
        "window_days": window_days,
        "session_logs": scanned,
        "per_type": dict(sorted(per_type.items())),
        "fill_rate": fill_rate,
        "sections": {
            h: {"present": have[h], "filled": filled[h]} for h in sorted(have)
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=90, help="0 scans everything")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    stats = collect(args.window_days)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
        return 0
    print(f"session logs ({args.window_days}d window): {stats['session_logs']}")
    for kind, count in stats["per_type"].items():
        print(f"  {kind}: {count}")
    print("lowest fill rates:")
    for heading, rate in sorted(stats["fill_rate"].items(), key=lambda kv: kv[1])[:8]:
        print(f"  {rate:>4.0%}  {heading}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
