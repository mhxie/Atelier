#!/usr/bin/env python3
"""deadlines.py: Read-only view over the dated-obligation index.

Why this exists: the things that must not be missed -- an expiring card credit,
an open equity selling window, a document expiry, a tax deadline -- are recorded
in prose inside the `finance/` and `travel/` trackers, mid-sentence and in mixed
languages, e.g. a bullet that ends "...expires <date>, deadline = 12/31".

A daily briefing that wants those dates would otherwise re-read several prose
trackers every morning and re-derive the same handful of dates, slowly and
non-deterministically. That is the exact re-derivation this harness avoids
elsewhere with aggregates plus a freshness gate.

So: judgment weekly, mechanics daily. A weekly pass extracts candidate rows
from the prose trackers, the user approves them, and the orchestrator writes
`$OV/_meta/deadlines.toml`. This script only reads that index -- there is no
write path here on purpose, because every row is a factual claim about the
user's money or documents and must pass through the normal $OV approval gate.

Every row carries `source = "<vault-relative path>:<line>"`. A row without a
resolvable source is a lint error, not a warning: an invented deadline on the
morning screen is worse than a missing one.

Freshness: `[meta] refreshed` plus `max_age_days` gate the whole index. When the
index is stale every output says so and `daily_brief.py` surfaces it as a
warning, rather than presenting month-old extraction as current.

Schema:

    [meta]
    refreshed = 2026-08-31
    max_age_days = 10

    [[deadline]]
    slug = "hotel-credit-unused"
    label = "Annual hotel credit, one use unspent"
    due = 2099-12-31
    kind = "perk"              # perk|window|ticket|tax|obligation|event|milestone
    reversible = false         # false = missing it forfeits the value
    source = "finance/example-tracker.md:107"
    action = "book one standalone night; do not stack"   # optional
    lead_days = 60             # optional; how early it needs to surface
    status = "open"            # open|done|dropped   (default open)

`lead_days` exists because a uniform horizon is wrong for this data. A credit
that only needs a card swipe is actionable with a week's notice; an award night
that needs a hotel booked is not, and surfacing it seven days out is the same as
not surfacing it. Each row declares its own lead time, defaulting to
DEFAULT_LEAD_DAYS.

Subcommands:
    list    every open row with computed state, newest deadline first
    due     rows closing within N days (default 14)
    lint    schema and provenance validation; non-zero exit on any error

Exit codes: 0 for list/due even when the index is missing (an absent index is a
warning the caller surfaces, not a crash). lint exits 1 on any error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import PathsError, fmt, vault_root  # noqa: E402

INDEX_RELPATH = "_meta/deadlines.toml"
DEFAULT_MAX_AGE_DAYS = 10
DEFAULT_DUE_WITHIN = 14
DEFAULT_LEAD_DAYS = 7

KINDS = {"perk", "window", "ticket", "tax", "obligation", "event", "milestone"}

# `milestone` is a dated review point on the quarter's main line: the rows of
# the commitments table in profile/directions.md that carry a date. Nothing is
# forfeited when one passes, so it never competes with perks; daily_brief.py
# gives it its own 本季主线 slot instead. Refreshed by /weekly, not by /digest.

# Kinds whose value is forfeited rather than delayed. Used by daily_brief.py to
# decide what earns a line on the morning screen.
FORFEITABLE_KINDS = {"perk", "window", "ticket"}

_SOURCE_RE = re.compile(r"^(?P<path>[^:]+\.md):(?P<line>\d+)$")


@dataclass
class Deadline:
    slug: str
    label: str
    due: str
    kind: str
    reversible: bool
    source: str
    action: str = ""
    status: str = "open"
    lead_days: int = DEFAULT_LEAD_DAYS
    days_left: int = 0
    state: str = "later"  # expired|today|soon|later

    def is_forfeitable(self) -> bool:
        if self.kind == "milestone":
            return False
        return not self.reversible or self.kind in FORFEITABLE_KINDS

    def in_lead_window(self) -> bool:
        """True once this row needs to start competing for attention."""
        return self.days_left <= self.lead_days


@dataclass
class Index:
    path: str
    exists: bool
    refreshed: str | None
    max_age_days: int
    age_days: int | None
    stale: bool
    deadlines: list[Deadline]
    errors: list[str]

    def warning(self) -> str | None:
        """One line the caller can put in front of a user, or None."""
        if not self.exists:
            return f"deadline index missing ({self.path}); dated perks and windows unknown"
        if self.refreshed is None:
            return f"deadline index has no [meta] refreshed date ({self.path})"
        if self.stale:
            return (
                f"deadline index stale {self.age_days}d "
                f"(limit {self.max_age_days}d); re-run the weekly extraction"
            )
        return None


def index_path(ov: Path) -> Path:
    return ov / INDEX_RELPATH


def load_index(ov: Path, today: date | None = None) -> Index:
    """Read and evaluate the index. Never raises on bad content.

    A malformed row is dropped into `errors` rather than aborting: one bad row
    must not blank the morning screen for every other row.
    """
    today = today or date.today()
    path = index_path(ov)
    if not path.is_file():
        return Index(
            path=INDEX_RELPATH,
            exists=False,
            refreshed=None,
            max_age_days=DEFAULT_MAX_AGE_DAYS,
            age_days=None,
            stale=True,
            deadlines=[],
            errors=[],
        )

    errors: list[str] = []
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return Index(
            path=INDEX_RELPATH,
            exists=True,
            refreshed=None,
            max_age_days=DEFAULT_MAX_AGE_DAYS,
            age_days=None,
            stale=True,
            deadlines=[],
            errors=[f"index unreadable: {exc!r}"],
        )

    meta = raw.get("meta") or {}
    refreshed = _coerce_date(meta.get("refreshed"))
    max_age = meta.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    if not isinstance(max_age, int) or max_age < 1:
        errors.append(f"[meta] max_age_days must be a positive int, got {max_age!r}")
        max_age = DEFAULT_MAX_AGE_DAYS
    age_days = (today - refreshed).days if refreshed else None
    stale = age_days is None or age_days > max_age

    deadlines: list[Deadline] = []
    seen: set[str] = set()
    for position, row in enumerate(raw.get("deadline") or [], start=1):
        item, row_errors = _build(row, position, today)
        errors.extend(row_errors)
        if item is None:
            continue
        if item.slug in seen:
            errors.append(f"deadline #{position}: duplicate slug {item.slug!r}")
            continue
        seen.add(item.slug)
        deadlines.append(item)

    deadlines.sort(key=lambda d: (d.due, d.slug))
    return Index(
        path=INDEX_RELPATH,
        exists=True,
        refreshed=refreshed.isoformat() if refreshed else None,
        max_age_days=max_age,
        age_days=age_days,
        stale=stale,
        deadlines=deadlines,
        errors=errors,
    )


def _build(row: Any, position: int, today: date) -> tuple[Deadline | None, list[str]]:
    errors: list[str] = []
    if not isinstance(row, dict):
        return None, [f"deadline #{position}: not a table"]

    slug = str(row.get("slug") or "").strip()
    label = str(row.get("label") or "").strip()
    due = _coerce_date(row.get("due"))
    kind = str(row.get("kind") or "").strip()
    source = str(row.get("source") or "").strip()

    where = f"deadline #{position}" + (f" ({slug})" if slug else "")
    if not slug:
        errors.append(f"{where}: missing slug")
    if not label:
        errors.append(f"{where}: missing label")
    if due is None:
        errors.append(f"{where}: missing or unparseable due date {row.get('due')!r}")
    if kind not in KINDS:
        errors.append(f"{where}: kind {kind!r} not in {sorted(KINDS)}")
    if not source:
        errors.append(f"{where}: missing source (every row needs <path>:<line> provenance)")
    elif not _SOURCE_RE.match(source):
        errors.append(f"{where}: source {source!r} is not <vault-relative .md path>:<line>")

    lead_days = row.get("lead_days", DEFAULT_LEAD_DAYS)
    if not isinstance(lead_days, int) or isinstance(lead_days, bool) or lead_days < 0:
        errors.append(f"{where}: lead_days must be a non-negative int, got {lead_days!r}")
        lead_days = DEFAULT_LEAD_DAYS

    status = str(row.get("status") or "open").strip()
    if status not in {"open", "done", "dropped"}:
        errors.append(f"{where}: status {status!r} not in open|done|dropped")
        status = "open"

    reversible = row.get("reversible")
    if reversible is None:
        errors.append(f"{where}: missing reversible (true|false)")
        reversible = True
    if not isinstance(reversible, bool):
        errors.append(f"{where}: reversible must be a boolean, got {reversible!r}")
        reversible = bool(reversible)

    if not slug or due is None:
        return None, errors

    days_left = (due - today).days
    return (
        Deadline(
            slug=slug,
            label=label or slug,
            due=due.isoformat(),
            kind=kind if kind in KINDS else "obligation",
            reversible=reversible,
            source=source,
            action=str(row.get("action") or "").strip(),
            status=status,
            lead_days=lead_days,
            days_left=days_left,
            state=_state(days_left),
        ),
        errors,
    )


def _state(days_left: int) -> str:
    if days_left < 0:
        return "expired"
    if days_left == 0:
        return "today"
    if days_left <= 7:
        return "soon"
    return "later"


def _coerce_date(value: Any) -> date | None:
    """Accept a TOML date, a datetime, or a YYYY-MM-DD string."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def open_deadlines(index: Index) -> list[Deadline]:
    return [d for d in index.deadlines if d.status == "open"]


def closing_within(index: Index, days: int) -> list[Deadline]:
    """Open rows already expired or closing within `days`, soonest first."""
    return [d for d in open_deadlines(index) if d.days_left <= days]


def in_lead_window(index: Index) -> list[Deadline]:
    """Open rows inside their own declared lead time, soonest first."""
    return [d for d in open_deadlines(index) if d.in_lead_window()]


# ---------------------------------------------------------------- cli


def _print_rows(rows: list[Deadline]) -> None:
    if not rows:
        print("  (none)")
        return
    for row in rows:
        when = (
            f"EXPIRED {-row.days_left}d"
            if row.days_left < 0
            else "TODAY" if row.days_left == 0 else f"{row.days_left}d"
        )
        forfeit = " [forfeitable]" if row.is_forfeitable() else ""
        lead = " [in lead window]" if row.in_lead_window() else ""
        print(f"  {when:>13}  {row.due}  {row.kind:<10} {row.label}{forfeit}{lead}")
        if row.action:
            print(f"                 ↳ {row.action}")
        print(f"                 ↳ {row.source}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the dated-obligation index.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List open deadlines.")
    p_list.add_argument("--kind", choices=sorted(KINDS), help="Filter by kind.")
    p_list.add_argument("--all", action="store_true", help="Include done and dropped rows.")
    p_list.add_argument("--json", action="store_true")

    p_due = sub.add_parser("due", help="List deadlines closing soon.")
    p_due.add_argument("--within", type=int, default=DEFAULT_DUE_WITHIN)
    p_due.add_argument(
        "--lead", action="store_true", help="Use each row's own lead_days instead of --within."
    )
    p_due.add_argument("--json", action="store_true")

    sub.add_parser("lint", help="Validate the index; non-zero exit on any error.")

    args = parser.parse_args(argv)

    try:
        ov = vault_root()
    except PathsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    today = date.today()
    index = load_index(ov, today)

    if args.cmd == "lint":
        if not index.exists:
            print(f"index absent: {fmt(index_path(ov))}")
            return 0
        for error in index.errors:
            print(f"ERROR {error}", file=sys.stderr)
        unresolved = _unresolved_sources(ov, index)
        for slug, source in unresolved:
            print(f"ERROR {slug}: source not found in vault: {source}", file=sys.stderr)
        warning = index.warning()
        if warning:
            print(f"WARN  {warning}")
        total = len(index.errors) + len(unresolved)
        print(f"{len(index.deadlines)} rows, {total} errors")
        return 1 if total else 0

    rows = index.deadlines if getattr(args, "all", False) else open_deadlines(index)
    if args.cmd == "due":
        rows = (
            [r for r in rows if r.in_lead_window()]
            if getattr(args, "lead", False)
            else [r for r in rows if r.days_left <= args.within]
        )
    if getattr(args, "kind", None):
        rows = [r for r in rows if r.kind == args.kind]

    if args.json:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "today": today.isoformat(),
                    "index": {
                        "path": index.path,
                        "exists": index.exists,
                        "refreshed": index.refreshed,
                        "age_days": index.age_days,
                        "stale": index.stale,
                        "warning": index.warning(),
                        "errors": index.errors,
                    },
                    "deadlines": [asdict(r) for r in rows],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    warning = index.warning()
    if warning:
        print(f"WARN  {warning}\n")
    _print_rows(rows)
    if index.errors:
        print(f"\n{len(index.errors)} schema errors; run `deadlines.py lint`")
    return 0


def _unresolved_sources(ov: Path, index: Index) -> list[tuple[str, str]]:
    """Rows whose `source` file does not exist, or whose line number runs
    past the end of it. Provenance that does not resolve is indistinguishable
    from an invented row."""
    missing: list[tuple[str, str]] = []
    for row in index.deadlines:
        match = _SOURCE_RE.match(row.source)
        if not match:
            continue
        path = ov / match.group("path")
        if not path.is_file():
            missing.append((row.slug, row.source))
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                line_count = sum(1 for _ in handle)
        except OSError:
            missing.append((row.slug, row.source))
            continue
        if int(match.group("line")) > line_count:
            missing.append((row.slug, f"{row.source} (line past end of file, {line_count} lines)"))
    return missing


if __name__ == "__main__":
    sys.exit(main())
