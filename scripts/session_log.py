#!/usr/bin/env python3
"""Create a uniquely named session-log skeleton for the orchestrator to fill."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import effective_date, tier, fmt  # type: ignore[import-not-found]  # noqa: E402

SESSIONS_DIR = tier("sessions")

VALID_TYPES = {
    "reflection",
    "review",
    "weekly",
    "decision",
    "exploration",
    "energy-audit",
    "reading",
    "curate",
    "introspect",
    "meeting",
    "deep-dive",
    "prm",
}

HEADER = """\
---session-log---
session_id: {session_id}
date: {date}
type: {session_type}
duration_estimate: {duration}
model: {model}
---end-session-log-header---

"""

FULL_SECTIONS = """\
## Agents Dispatched
| Agent | Task | Result | Turns Used |
|-------|------|--------|------------|

## Search Log
| Query | Tool | Hits | Top Result | Useful |
|-------|------|------|------------|--------|

## Gate Results
| Gate | Score/Pass | Notes |
|------|-----------|-------|

## Questions & Engagement
| Question | Depth | Landed | User Response |
|----------|-------|--------|---------------|

## Frameworks Applied
| Framework | Applied By | Fit Score | Cross-validated |
|-----------|-----------|-----------|-----------------|

## Continuity
- Previous session referenced: none
- Seed planted:
- Callbacks checked:

## Decisions & Branches

## Anomalies

## Harness Assumptions Exercised
"""

COMPACT_SECTIONS = """\
## Continuity
- (none)

## Anomalies
- (none)

## Operations
- (none)
"""

COMPACT_TYPES = {
    "reflection",
    "weekly",
    "review",
    "decision",
    "energy-audit",
    "exploration",
}


def _write_next_log(
    session_type: str,
    today: date,
    *,
    duration: int,
    model: str,
) -> Path:
    """Create the next log exclusively so collisions cannot overwrite data."""
    base_id = f"{today.isoformat()}-{session_type}"
    sequence = 1
    while True:
        session_id = base_id if sequence == 1 else f"{base_id}-{sequence}"
        path = SESSIONS_DIR / f"{session_id}.md"
        content = HEADER.format(
            session_id=session_id,
            date=today.isoformat(),
            session_type=session_type,
            duration=duration,
            model=model,
        ) + (COMPACT_SECTIONS if session_type in COMPACT_TYPES else FULL_SECTIONS)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError:
            sequence += 1
            continue
        return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/session_log.py",
        description="Create a session log skeleton.",
    )
    parser.add_argument(
        "--type",
        required=True,
        choices=sorted(VALID_TYPES),
        help="Session type.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Estimated session duration in minutes.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Orchestrator model used.",
    )
    args = parser.parse_args(argv)

    if args.type == "reading":
        sys.stderr.write(
            "reading logs must be created complete in one file operation; "
            "follow protocols/session-log.md\n"
        )
        return 2

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    path = _write_next_log(
        args.type,
        effective_date(),
        duration=args.duration,
        model=args.model,
    )
    sys.stdout.write(fmt(path) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
