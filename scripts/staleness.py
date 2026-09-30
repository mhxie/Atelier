#!/usr/bin/env python3
"""Score working-layer notes for archival, compaction, or promotion review.

The mtime-based score discounts age by inbound references. Thresholds classify
stale and dormant notes; old referenced notes without a wiki entry become
promotion candidates. Results are advisory and never make the command fail.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier, tier_files  # type: ignore[import-not-found]  # noqa: E402

# L2 directories to scan (daily notes excluded: capture stream, not working
# knowledge). health excluded: longitudinal records, different lifecycle.
# Paths resolve via the canonical path registry so renames in harness/paths.toml
# propagate automatically.
L2_DIRS = [
    tier("agent_findings"),
    tier("wip"),
    tier("research"),
    tier("gtd"),
    tier("preprints"),
    tier("reflections"),
]

WIKI_DIR = tier("wiki")

STALE_THRESHOLD = 90
DORMANT_THRESHOLD = 45
PROMOTION_AGE_DAYS = 180
PROMOTION_MIN_REFS = 2


@dataclass(slots=True, eq=False, repr=False)
class NoteScore:
    path: Path
    days_since_modified: int
    reference_count: int
    staleness: float
    category: str
    has_wiki_entry: bool

    def to_dict(self) -> dict:
        return {
            "path": self.path.as_posix(),
            "days_since_modified": self.days_since_modified,
            "reference_count": self.reference_count,
            "staleness": round(self.staleness, 1),
            "category": self.category,
            "has_wiki_entry": self.has_wiki_entry,
        }


def _days_since_modified(path: Path, today: date) -> int:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 9999
    from datetime import datetime

    mod_date = datetime.fromtimestamp(mtime).date()
    return max(0, (today - mod_date).days)


def _count_references(stem: str, title_line: str | None, corpus_files: list[Path]) -> int:
    """Count how many files in the corpus reference this note by stem or title.

    Uses simple substring matching. Good enough for v1; semantic.py can
    replace this in v2.
    """
    count = 0
    needles = [stem]
    if title_line:
        needles.append(title_line)
    for f in corpus_files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for needle in needles:
            if needle in text:
                count += 1
                break
    return count


def _extract_title(path: Path) -> str | None:
    """Extract H1 title from a markdown file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("# ") and not stripped.startswith("## "):
                    return stripped[2:].strip()
                if stripped and not stripped.startswith("---"):
                    break
    except OSError:
        pass
    return None


def _compute_staleness(days: int, refs: int) -> float:
    """staleness = days / (1 + log(1 + refs))"""
    return days / (1.0 + math.log(1.0 + refs))


def _categorize(score: NoteScore) -> str:
    """Assign an actionable category."""
    if (
        score.days_since_modified >= PROMOTION_AGE_DAYS
        and score.reference_count >= PROMOTION_MIN_REFS
        and not score.has_wiki_entry
    ):
        return "promote"
    if score.staleness >= STALE_THRESHOLD:
        return "stale"
    if score.staleness >= DORMANT_THRESHOLD:
        return "dormant"
    return "active"


def scan(
    dirs: list[Path],
    today: date,
) -> list[NoteScore]:
    """Scan L2 directories and score each note."""
    corpus: list[Path] = []
    if WIKI_DIR.exists():
        corpus.extend(WIKI_DIR.rglob("*.md"))

    corpus.extend(tier_files("reflections", "*.md"))

    # Last 30 days of daily notes for recency-weighted reference counting.
    daily_dir = tier("daily_notes")
    if daily_dir.exists():
        for i in range(30):
            d = today - timedelta(days=i)
            p = daily_dir / f"{d.isoformat()}.md"
            if p.exists():
                corpus.append(p)

    wiki_stems: set[str] = set()
    if WIKI_DIR.exists():
        for p in WIKI_DIR.rglob("*.md"):
            if p.name != "index.md":
                wiki_stems.add(p.stem)

    results: list[NoteScore] = []
    for d in dirs:
        if not d.exists():
            continue
        # Fission-aware: L2 tiers bucket into subdirectories (reflections by
        # year-month, agent-findings topically); a flat glob went blind here
        # for the third time on 2026-08-23.
        for path in sorted(d.rglob("*.md")):
            days = _days_since_modified(path, today)
            title = _extract_title(path)

            # Exclude the note's own file from the corpus to avoid self-reference.
            ref_corpus = [f for f in corpus if f.resolve() != path.resolve()]
            refs = _count_references(path.stem, title, ref_corpus)

            staleness = _compute_staleness(days, refs)

            has_wiki = path.stem in wiki_stems

            ns = NoteScore(
                path=path,
                days_since_modified=days,
                reference_count=refs,
                staleness=staleness,
                category="",
                has_wiki_entry=has_wiki,
            )
            ns.category = _categorize(ns)
            results.append(ns)

    results.sort(key=lambda s: (-s.staleness, s.path.as_posix()))
    return results


def format_table(scores: list[NoteScore]) -> str:
    if not scores:
        return "staleness: no L2 notes found\n"

    counts = {"stale": 0, "dormant": 0, "active": 0, "promote": 0}
    for s in scores:
        counts[s.category] = counts.get(s.category, 0) + 1

    lines: list[str] = []
    lines.append(
        f"staleness report: {counts['stale']} stale, {counts['dormant']} dormant, "
        f"{counts['promote']} promote, {counts['active']} active "
        f"({len(scores)} total)"
    )
    lines.append("")

    actionable = [s for s in scores if s.category != "active"]
    if not actionable:
        lines.append("All L2 notes are active. Nothing to do.")
        return "\n".join(lines) + "\n"

    header = f"{'score':>6}  {'days':>5}  {'refs':>4}  {'category':<8}  path"
    lines.append(header)
    lines.append("-" * len(header))

    for s in actionable:
        lines.append(
            f"{s.staleness:6.1f}  {s.days_since_modified:5d}  {s.reference_count:4d}  {s.category:<8}  {s.path.as_posix()}"
        )

    lines.append("")
    lines.append("Actions:")
    if counts["stale"]:
        lines.append(f"  stale ({counts['stale']}): consider archiving to zk/archive/")
    if counts["dormant"]:
        lines.append(f"  dormant ({counts['dormant']}): review or compact")
    if counts["promote"]:
        lines.append(
            f"  promote ({counts['promote']}): well-referenced but no wiki entry; run /promote"
        )
    return "\n".join(lines) + "\n"


def format_json(scores: list[NoteScore]) -> str:
    payload = {
        "l2_dirs": [d.as_posix() for d in L2_DIRS],
        "thresholds": {
            "stale": STALE_THRESHOLD,
            "dormant": DORMANT_THRESHOLD,
            "promotion_age_days": PROMOTION_AGE_DAYS,
            "promotion_min_refs": PROMOTION_MIN_REFS,
        },
        "counts": {
            "stale": sum(1 for s in scores if s.category == "stale"),
            "dormant": sum(1 for s in scores if s.category == "dormant"),
            "promote": sum(1 for s in scores if s.category == "promote"),
            "active": sum(1 for s in scores if s.category == "active"),
            "total": len(scores),
        },
        "notes": [s.to_dict() for s in scores],
    }
    return json.dumps(payload, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/staleness.py",
        description="Active forgetting: staleness scoring for L2 working-layer notes.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON for orchestrator consumption.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Scan a single L2 directory instead of all.",
    )
    args = parser.parse_args(argv)

    dirs = [args.dir] if args.dir else L2_DIRS
    today = date.today()
    scores = scan(dirs, today)

    if args.json:
        sys.stdout.write(format_json(scores))
    else:
        sys.stdout.write(format_table(scores))

    return 0


if __name__ == "__main__":
    sys.exit(main())
