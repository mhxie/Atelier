#!/usr/bin/env python3
"""Log-side dining aggregates: the deterministic half of /dine Intent A.

The dine flow re-read the whole meal-history tracker with a top-tier model on
every run, so its cost grew linearly with dining history. This script is the
sole owner of the log-derived facts and score components; the table in
`.claude/commands/dine.md` § Step 3 consumes `log_score` and keeps only the
catalog-side factors (场景索引, credit cycles, Michelin moods).

Emits one JSON object:
  restaurants   per-restaurant aggregates + log-side score component
  excluded      visited within --avoid-days (hard filter)
  sourced_rows  how many tracker rows fed the aggregates

Reuses `dining_audit`'s canonical-schema parsing so the two never drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import vault_root  # noqa: E402
import dining_audit as da  # noqa: E402


def _find_meal_history(profile_path: Path, vault: Path) -> Path | None:
    mappings, _ = da._parse_catalog_paths(profile_path, vault)
    for role, path in mappings.items():
        if "meal" in role.lower() or "history" in role.lower():
            return path
    return None


def _parse_rows(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header_indexes = [
        i for i, line in enumerate(lines)
        if tuple(da._split_markdown_row(line)) == da.EXPECTED_COLUMNS
    ]
    if len(header_indexes) != 1:
        raise SystemExit("meal history lacks exactly one canonical table (run dining_audit)")
    rows = []
    for line in lines[header_indexes[0] + 2 :]:
        if not line.strip().startswith("|"):
            break
        cells = da._split_markdown_row(line)
        if len(cells) != len(da.EXPECTED_COLUMNS):
            continue
        row: dict[str, object] = dict(zip(da.EXPECTED_COLUMNS, cells))
        m = da.DATE_RE.search(str(row.get("Date", "")))
        if not m:
            continue
        row["_date"] = date.fromisoformat(m.group(1))
        rows.append(row)
    return rows


def _score(avg_recent: float | None, last_again: str | None, days_since: int | None) -> tuple[int, list[str]]:
    """Log-side components from dine.md § Step 3 (catalog side stays with the model)."""
    score, parts = 0, []
    if avg_recent is not None:
        if avg_recent >= 8:
            score += 5
            parts.append("log avg >=8: +5")
        elif avg_recent >= 6:
            score += 2
            parts.append("log avg 6-7: +2")
        elif avg_recent <= 5:
            score -= 3
            parts.append("log avg <=5: -3")
    if last_again == "Y":
        score += 2
        parts.append("再去 Y: +2")
    elif last_again == "N":
        score -= 5
        parts.append("再去 N: -5")
    if days_since is not None and days_since > 90:
        score += 1
        parts.append("rusty >90d: +1")
    return score, parts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--avoid-days", type=int, default=30)
    parser.add_argument("--today", default=None, help="YYYY-MM-DD (tests)")
    parser.add_argument("--tracker", default=None, help="Explicit tracker path (bypasses profile resolution)")
    args = parser.parse_args(argv)

    vault = vault_root()
    today = date.fromisoformat(args.today) if args.today else date.today()
    if args.tracker:
        tracker = Path(args.tracker)
    else:
        profile = Path(__file__).resolve().parents[1] / "profile" / "diet.md"
        tracker = _find_meal_history(profile, vault)
    if tracker is None or not tracker.is_file():
        print(json.dumps({"error": "meal-history tracker not resolvable; pass --tracker"}))
        return 2

    rows = _parse_rows(tracker)
    by_restaurant: dict[str, list[dict]] = {}
    for row in rows:
        name = str(row.get("Restaurant", "")).strip()
        if name and name not in da.UNKNOWN:
            by_restaurant.setdefault(name, []).append(row)

    cutoff = today - timedelta(days=args.avoid_days)
    restaurants, excluded = {}, []
    for name, visits in sorted(by_restaurant.items()):
        visits.sort(key=lambda r: r["_date"])
        last = visits[-1]
        days_since = (today - last["_date"]).days
        ratings = []
        for row in visits[-3:]:
            raw = str(row.get("评分", "")).strip()
            try:
                ratings.append(float(raw))
            except ValueError:
                continue
        avg_recent = round(sum(ratings) / len(ratings), 2) if ratings else None
        # 再去 goes unfilled once a restaurant is settled (profile/diet.md
        # "Capture tiers"), so carry the most recent decided value forward
        # instead of reading only the newest row.
        last_again = None
        for row in reversed(visits):
            candidate = str(row.get("再去", "")).strip()
            if candidate and candidate not in da.UNKNOWN:
                last_again = candidate
                break
        score, parts = _score(avg_recent, last_again, days_since)
        entry = {
            "visits": len(visits),
            "last_visit": last["_date"].isoformat(),
            "days_since": days_since,
            "avg_rating_last3": avg_recent,
            "last_again": last_again,
            "log_score": score,
            "log_score_parts": parts,
        }
        if last["_date"] >= cutoff:
            excluded.append(name)
            entry["excluded"] = f"visited within {args.avoid_days}d"
        restaurants[name] = entry

    print(json.dumps({
        "restaurants": restaurants,
        "excluded": sorted(excluded),
        "sourced_rows": len(rows),
        "avoid_days": args.avoid_days,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
