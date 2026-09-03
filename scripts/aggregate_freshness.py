#!/usr/bin/env python3
"""
aggregate_freshness.py: detect aggregate-vs-detail staleness.

Problem this exists for. Atelier's L1-L5 axis describes *certification depth*
but is silent about *aggregation*. Detail files (e.g. `travel/trips/<trip>.md`)
are the source of truth for a specific subject; aggregate trackers (e.g.
`travel/<calendar>.md`, `travel/<inventory>.md`, and
`finance/<benefits-tracker>.md`) are hand-mirrored views over many subjects. When the user
updates a detail file, nothing pushes back to the aggregates, so read commands
Read workflows can surface stale facts as authoritative. This is
antipattern #6 (shadow state) at the data layer.

This script is a read-time guard. Given a subjects directory and a list of
aggregate files, it reports any aggregate whose `Last updated:` line is older
than the newest subject's `Last updated:` line. Read commands call it as a
pre-step and surface divergence to the user before quoting aggregate values.

The script doesn't fix the divergence (that requires human judgement about
which fields in the aggregate were derived from which subject). It just makes
the divergence loud at read time.

Timestamp source. The script looks for an explicit signal first and falls
back to filesystem mtime so it stays useful on files the user hasn't tagged
manually:
  1. A line of the form `Last updated: YYYY-MM-DD` within the first 10 lines.
  2. A YAML frontmatter key `last_updated: YYYY-MM-DD` (or `updated:`).
  3. Filesystem mtime.

Aggregates typically carry the explicit `Last updated:` line (it's part of
the tracker convention). Detail files often don't — that's fine, mtime is a
better signal for "did the user edit this since the aggregate was refreshed?"
anyway.

CLI:
    aggregate_freshness.py \
        --subjects travel/trips \
        --aggregates travel/<calendar>.md travel/<inventory>.md \
            finance/<benefits-tracker>.md \
        --json

    # Or walk $OV automatically for files declaring themselves aggregates:
    aggregate_freshness.py --discover [--stale-only]

Discovery mode. With `--discover`, the script walks `$OV` looking for files
whose YAML frontmatter contains `freshness: required` and a `subjects:` key
pointing at a directory. It groups aggregates by their declared subjects dir
and runs the same comparison the explicit-args path does. `--stale-only`
filters the output to entries flagged stale (useful for session-start cues:
silent when everything is fresh).

Exit code: 0 always. Staleness is advisory; the caller decides what to do
with the JSON. (Non-zero exit would block read commands, which is worse than
showing stale data with a warning.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import fmt, parse_iso_date, vault_root  # type: ignore[import-not-found]  # noqa: E402

_LAST_UPDATED_RE = re.compile(r"^Last updated:\s*(\d{4}-\d{2}-\d{2})\s*$")
_YAML_UPDATED_RE = re.compile(r"^(?:last_updated|updated):\s*(\d{4}-\d{2}-\d{2})\s*$")
_FRESHNESS_REQ_RE = re.compile(r"^freshness:\s*required\s*$")
_SUBJECTS_RE = re.compile(r"^subjects:\s*(.+?)\s*$")
_HEAD_LINES = 20
# Directories to skip when walking $OV for --discover (large/irrelevant trees).
_DISCOVER_SKIP_DIRS = {
    ".git", ".obsidian", "cache", "papers", "preprints", "archive", "zettelm",
    "node_modules", ".venv", "__pycache__",
}


def _parse_iso(s: str) -> date | None:
    return parse_iso_date(s)


def _read_last_updated(path: Path) -> tuple[date, str] | None:
    """Resolve a file's last-updated date.

    Returns (date, source) where source is one of "marker", "yaml", "mtime".
    Falls back to mtime so files without an explicit marker still produce a
    comparable signal.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _HEAD_LINES:
                    break
                stripped = line.rstrip()
                m = _LAST_UPDATED_RE.match(stripped)
                if m:
                    d = _parse_iso(m.group(1))
                    if d:
                        return d, "marker"
                m = _YAML_UPDATED_RE.match(stripped)
                if m:
                    d = _parse_iso(m.group(1))
                    if d:
                        return d, "yaml"
    except OSError:
        return None

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return datetime.fromtimestamp(mtime).date(), "mtime"


def _resolve(p: str) -> Path:
    """Resolve a CLI path argument under the vault root unless absolute."""
    path = Path(p).expanduser()
    if path.is_absolute():
        return path
    return vault_root() / path


def _read_aggregate_frontmatter(path: Path) -> dict | None:
    """Extract `subjects:` and `freshness:` from a file's YAML frontmatter.

    Returns {"subjects": <str>, "freshness": "required"} if both keys are
    present in a leading `---`-fenced YAML block; None otherwise. Only the
    first frontmatter block is consulted; only the keys we care about are
    parsed (no full YAML loader needed).
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.rstrip() != "---":
                return None
            data: dict = {}
            for _ in range(_HEAD_LINES):
                line = fh.readline()
                if not line:
                    return None
                stripped = line.rstrip()
                if stripped == "---":
                    break
                if _FRESHNESS_REQ_RE.match(stripped):
                    data["freshness"] = "required"
                    continue
                m = _SUBJECTS_RE.match(stripped)
                if m:
                    data["subjects"] = m.group(1).strip()
            if data.get("freshness") == "required" and data.get("subjects"):
                return data
            return None
    except OSError:
        return None


def discover(stale_only: bool = False, verbose: bool = False) -> dict:
    """Walk $OV, find self-declared aggregates, group by subjects dir, scan.

    Returns:
        {"groups": [<scan-payload>, ...], "discovered": N, "stale_count": M}
    Each group payload matches `scan()`'s return shape.
    """
    root = vault_root()
    pairs: dict[str, list[Path]] = {}  # subjects_str -> [aggregate paths]
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place.
        dirnames[:] = [d for d in dirnames if d not in _DISCOVER_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            p = Path(dirpath) / fn
            fm = _read_aggregate_frontmatter(p)
            if fm is None:
                continue
            pairs.setdefault(fm["subjects"], []).append(p)

    groups: list[dict] = []
    stale_total = 0
    for subj_str, aggs in sorted(pairs.items()):
        subj_dir = _resolve(subj_str)
        payload = scan(subj_dir, sorted(aggs), verbose=verbose)
        stale_here = [a for a in payload["aggregates"] if a.get("stale")]
        stale_total += len(stale_here)
        if stale_only:
            if not stale_here:
                continue
            payload = dict(payload)
            payload["aggregates"] = stale_here
        groups.append(payload)

    return {
        "groups": groups,
        "discovered": sum(len(v) for v in pairs.values()),
        "stale_count": stale_total,
    }


def scan(
    subjects_dir: Path,
    aggregates: list[Path],
    verbose: bool = False,
) -> dict:
    """Compare aggregate timestamps to the newest subject timestamp.

    Returns a structured payload:
        {
          "subjects_dir": "$OV/travel/trips",
          "newest_subject": {"path": ..., "last_updated": "YYYY-MM-DD"},
          "aggregates": [
            {"path": ..., "last_updated": ..., "stale": bool, "days_behind": int}
          ],
          "warnings": [...]
        }
    """
    warnings: list[str] = []

    # Collect subject files and their Last-updated dates.
    subjects: list[tuple[Path, date, str]] = []
    if not subjects_dir.exists():
        warnings.append(f"subjects_dir does not exist: {fmt(subjects_dir)}")
    else:
        for sp in sorted(subjects_dir.glob("*.md")):
            r = _read_last_updated(sp)
            if r is None:
                if verbose:
                    warnings.append(f"no timestamp resolvable: {fmt(sp)}")
                continue
            d, src = r
            subjects.append((sp, d, src))

    newest_subject: dict | None = None
    newest_date: date | None = None
    if subjects:
        sp, d, src = max(subjects, key=lambda t: t[1])
        newest_date = d
        newest_subject = {
            "path": fmt(sp),
            "last_updated": d.isoformat(),
            "source": src,
        }

    # Score each aggregate against the newest subject date.
    agg_results: list[dict] = []
    for ap in aggregates:
        if not ap.exists():
            agg_results.append(
                {
                    "path": fmt(ap),
                    "last_updated": None,
                    "source": None,
                    "stale": False,
                    "days_behind": None,
                    "note": "file not found",
                }
            )
            continue
        r = _read_last_updated(ap)
        if r is None:
            agg_results.append(
                {
                    "path": fmt(ap),
                    "last_updated": None,
                    "source": None,
                    "stale": False,
                    "days_behind": None,
                    "note": "no timestamp resolvable",
                }
            )
            continue
        ad, asrc = r
        if newest_date is None:
            agg_results.append(
                {
                    "path": fmt(ap),
                    "last_updated": ad.isoformat(),
                    "source": asrc,
                    "stale": False,
                    "days_behind": None,
                    "note": "no subjects to compare against",
                }
            )
            continue
        days_behind = (newest_date - ad).days
        agg_results.append(
            {
                "path": fmt(ap),
                "last_updated": ad.isoformat(),
                "source": asrc,
                "stale": days_behind > 0,
                "days_behind": days_behind,
            }
        )

    return {
        "subjects_dir": fmt(subjects_dir),
        "subject_count": len(subjects),
        "newest_subject": newest_subject,
        "aggregates": agg_results,
        "warnings": warnings,
    }


def format_human_discover(payload: dict, stale_only: bool) -> str:
    groups = payload["groups"]
    if not groups:
        if stale_only:
            return f"aggregate freshness: 0 stale of {payload['discovered']} discovered\n"
        return "aggregate freshness: 0 aggregates discovered under $OV\n"
    lines: list[str] = []
    header = (
        f"aggregate freshness ({payload['stale_count']} stale of "
        f"{payload['discovered']} discovered):"
    )
    lines.append(header)
    lines.append("")
    for g in groups:
        lines.append(format_human(g).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_human(payload: dict) -> str:
    lines: list[str] = []
    lines.append(f"aggregate freshness: subjects={payload['subjects_dir']}")
    ns = payload.get("newest_subject")
    if ns:
        lines.append(
            f"  newest subject: {ns['path']} ({ns['last_updated']} via {ns['source']})"
        )
    else:
        lines.append("  newest subject: (none found)")
    lines.append("")
    stale_count = sum(1 for a in payload["aggregates"] if a.get("stale"))
    lines.append(f"aggregates ({stale_count} stale of {len(payload['aggregates'])}):")
    for a in payload["aggregates"]:
        if a.get("stale"):
            marker = f"STALE (-{a['days_behind']}d)"
        elif a.get("note"):
            marker = a["note"]
        else:
            marker = "fresh"
        lu = a.get("last_updated") or "—"
        src = a.get("source")
        src_suffix = f" via {src}" if src else ""
        lines.append(f"  [{marker:>18}] {lu}{src_suffix}  {a['path']}")
    if payload.get("warnings"):
        lines.append("")
        lines.append("warnings:")
        for w in payload["warnings"]:
            lines.append(f"  - {w}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/aggregate_freshness.py",
        description="Detect aggregate trackers that lag the detail SOT files they summarize.",
    )
    parser.add_argument(
        "--subjects",
        help="Directory holding detail SOT files (e.g. travel/trips). Required unless --discover.",
    )
    parser.add_argument(
        "--aggregates",
        nargs="+",
        help="One or more aggregate tracker files (paths relative to $OV or absolute). Required unless --discover.",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Walk $OV for files with `freshness: required` + `subjects:` frontmatter; ignore --subjects/--aggregates.",
    )
    parser.add_argument(
        "--stale-only",
        action="store_true",
        help="Filter --discover output to stale aggregates only (silent when all fresh).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON for orchestrator consumption.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include warnings for files missing a Last-updated line.",
    )
    args = parser.parse_args(argv)

    if args.discover:
        payload = discover(stale_only=args.stale_only, verbose=args.verbose)
        if args.json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        else:
            sys.stdout.write(format_human_discover(payload, args.stale_only))
        return 0

    if not args.subjects or not args.aggregates:
        parser.error("--subjects and --aggregates are required unless --discover is given")

    subjects_dir = _resolve(args.subjects)
    aggregates = [_resolve(a) for a in args.aggregates]
    payload = scan(subjects_dir, aggregates, verbose=args.verbose)

    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(format_human(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
