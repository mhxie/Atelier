#!/usr/bin/env python3
"""Report aggregate files older than their source details.

Dates come from a leading ``Last updated:`` marker, YAML ``last_updated`` or
``updated``, then filesystem mtime. Discovery reads self-declared
``freshness: required`` aggregates. Findings are advisory and never rewrite
the divergent files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import fmt, parse_iso_date, vault_root  # type: ignore[import-not-found]  # noqa: E402

_LAST_UPDATED_RE = re.compile(r"^Last updated:\s*(\d{4}-\d{2}-\d{2})\s*$")
_YAML_UPDATED_RE = re.compile(r"^(?:last_updated|updated):\s*(\d{4}-\d{2}-\d{2})\s*$")
_HEAD_LINES = 20
_DISCOVER_SKIP_DIRS = {
    ".git", ".obsidian", "cache", "papers", "preprints", "archive",
    "node_modules", ".venv", "__pycache__",
}


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
                    d = parse_iso_date(m.group(1))
                    if d:
                        return d, "marker"
                m = _YAML_UPDATED_RE.match(stripped)
                if m:
                    d = parse_iso_date(m.group(1))
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
    """Read a bounded, closed YAML header declaring a required aggregate."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.rstrip() != "---":
                return None
            header: list[str] = []
            for _ in range(_HEAD_LINES):
                line = fh.readline()
                if not line:
                    return None
                stripped = line.rstrip()
                if stripped == "---":
                    data = yaml.load("".join(header), Loader=yaml.BaseLoader)
                    if (isinstance(data, dict) and data.get("freshness") == "required"
                            and isinstance(data.get("subjects"), str) and data["subjects"].strip()):
                        return {"subjects": data["subjects"], "freshness": "required"}
                    return None
                header.append(line)
            return None
    except (OSError, yaml.YAMLError):
        return None


def discover(stale_only: bool = False, verbose: bool = False) -> dict:
    """Walk $OV, find self-declared aggregates, group by subjects dir, scan.

    Returns:
        {"groups": [<scan-payload>, ...], "discovered": N, "stale_count": M}
    Each group payload matches `scan()`'s return shape.
    """
    root = vault_root()
    pairs: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
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
            if not stale_here and not payload["warnings"]:
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
    """Return the newest subject, per-aggregate staleness/days_behind, and warnings."""
    warnings: list[str] = []

    subjects: list[tuple[Path, date, str]] = []
    if not subjects_dir.is_dir():
        warnings.append(f"subjects_dir is missing or not a directory: {fmt(subjects_dir)}")
    else:
        aggregate_paths = {ap.resolve() for ap in aggregates}
        for sp in sorted(subjects_dir.rglob("*.md")):
            if sp.resolve() in aggregate_paths or any(p.startswith(".") for p in sp.relative_to(subjects_dir).parts):
                continue
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
