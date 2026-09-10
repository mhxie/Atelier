#!/usr/bin/env python3
"""Look up person notes without whitespace-sensitive shell pipelines.

Filename matching is always available. ``ATELIER_PEOPLE_NAME_FIELD`` may name
a private body-field label for opt-in non-English matching. Exit status
distinguishes matches, no matches, and invalid setup.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import fmt, tier  # type: ignore[import-not-found]  # noqa: E402

PEOPLE_DIR = tier("people")
HEAD_LINES = 30  # the bio block sits at the top of every person note

_NAME_FIELD = os.environ.get("ATELIER_PEOPLE_NAME_FIELD", "").strip()
_BODY_NAME_RE: re.Pattern[str] | None = (
    re.compile(rf"^\s*-\s*{re.escape(_NAME_FIELD)}\s*:\s*(\S.*?)\s*$")
    if _NAME_FIELD
    else None
)


def scan(query: str) -> list[dict]:
    if not PEOPLE_DIR.is_dir():
        print(f"ERROR: {fmt(PEOPLE_DIR)} not found.", file=sys.stderr)
        sys.exit(2)
    q_lower = query.lower()
    results: list[dict] = []
    # people/ is first-letter bucketed (repo-conventions § fission); a flat
    # glob misses every bucketed stub, which is how duplicates got created.
    for path in sorted(PEOPLE_DIR.rglob("*.md")):
        stem = path.stem
        match_src: str | None = None
        if q_lower in stem.lower():
            match_src = "filename"
        elif _BODY_NAME_RE is not None:
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    head = list(islice(handle, HEAD_LINES))
            except OSError:
                continue
            for line in head:
                m = _BODY_NAME_RE.match(line)
                if m and query in m.group(1):
                    match_src = "body"
                    break
        if match_src:
            results.append({"path": fmt(path), "match": match_src, "stem": stem})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/people.py",
        description="Search $OV/people/ by name fragment.",
    )
    parser.add_argument("query", help="Name fragment.")
    parser.add_argument("--json", action="store_true", help="JSON output.")
    args = parser.parse_args(argv)

    query = args.query.strip()
    if not query:
        print("ERROR: empty query", file=sys.stderr)
        sys.exit(2)

    results = scan(query)

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        for r in results:
            sys.stdout.write(f"{r['path']}\t[{r['match']}]\n")

    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
