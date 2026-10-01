#!/usr/bin/env python3
"""Manage recurring obligations in ``$OV/gtd/recurring.md``.

Rows declare ``every``, ``last-done``, and an optional area without a checkbox,
so the one-shot TODO scanner ignores them. Day, week, month, and year units use
fixed day counts by design. Overdue state is reportable rather than an error;
the cue layer decides when to surface it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write, tier  # type: ignore[import-not-found]  # noqa: E402

RECURRING_FILE_RELATIVE = "recurring.md"

ITEM_RE = re.compile(
    r"^- ([a-z0-9][\w-]*)"
    r"\s+every:(\d+)(d|w|mo|y)"
    r"\s+last-done:(\d{4}-\d{2}-\d{2})"
    r"(?:\s+area:(#[\w-]+))?"
    r"\s*$"
)
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")

UNIT_DAYS = {"d": 1, "w": 7, "mo": 30, "y": 365}


@dataclass
class Recurring:
    slug: str
    every_n: int
    every_unit: str
    last_done: str
    area: str | None
    section: str | None
    line: int

    def every_days(self) -> int:
        return self.every_n * UNIT_DAYS[self.every_unit]

    def last_done_date(self) -> date:
        return date.fromisoformat(self.last_done)

    def next_due(self) -> date:
        return self.last_done_date() + timedelta(days=self.every_days())

    def days_until_due(self, today: date) -> int:
        return (self.next_due() - today).days

    def status(self, today: date) -> str:
        d = self.days_until_due(today)
        if d < 0:
            return "overdue"
        if d <= 7:
            return "due-soon"
        return "satisfied"

    def every_str(self) -> str:
        return f"{self.every_n}{self.every_unit}"


def recurring_path() -> Path:
    return tier("gtd") / RECURRING_FILE_RELATIVE


def parse_file(errors: list[str] | None = None) -> list[Recurring]:
    path = recurring_path()
    if not path.is_file():
        return []
    items: list[Recurring] = []
    current_section: str | None = None
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        sec_m = SECTION_RE.match(line)
        if sec_m:
            current_section = sec_m.group(1)
            continue
        m = ITEM_RE.match(line)
        if not m:
            continue
        slug, n, unit, last_done, area = m.groups()
        try:
            item = Recurring(slug, int(n), unit, last_done, area, current_section, i)
            if item.every_n < 1:
                raise ValueError("interval must be positive")
            item.next_due()
        except (ValueError, OverflowError) as exc:
            warning = f"invalid recurring row {i} ({slug}): {exc}"
            if errors is None:
                print(warning, file=sys.stderr)
            else:
                errors.append(warning)
            continue
        items.append(item)
    return items


def find_by_slug(slug: str) -> Recurring | None:
    for item in parse_file():
        if item.slug == slug:
            return item
    return None


def update_last_done(slug: str, new_date: str) -> bool:
    path = recurring_path()
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    for i, line in enumerate(lines):
        m = ITEM_RE.match(line)
        if not m or m.group(1) != slug:
            continue
        completed = date.fromisoformat(new_date)
        interval = int(m.group(2)) * UNIT_DAYS[m.group(3)]
        if interval < 1:
            raise ValueError("interval must be positive")
        completed + timedelta(days=interval)
        new_date = completed.isoformat()
        lines[i] = re.sub(
            r"last-done:\d{4}-\d{2}-\d{2}",
            f"last-done:{new_date}",
            line,
            count=1,
        )
        changed = True
        break
    if changed:
        atomic_write(path, "\n".join(lines) + "\n")
    return changed


def cmd_list(args: argparse.Namespace) -> int:
    today = date.today()
    items = parse_file()
    if args.area:
        items = [i for i in items if i.area == args.area]

    visible = [i for i in items if i.status(today) != "satisfied" or args.all]
    visible.sort(key=lambda i: (i.next_due(), i.slug))

    if args.json:
        payload = []
        for i in visible:
            d = asdict(i)
            d["next_due"] = i.next_due().isoformat()
            d["days_until_due"] = i.days_until_due(today)
            d["status"] = i.status(today)
            payload.append(d)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not visible:
        if not items:
            print("No recurring items found. Add some to gtd/recurring.md.")
        else:
            print("All recurring items satisfied. Run with --all to see them.")
        return 0

    groups: dict[str, list[Recurring]] = {}
    for i in visible:
        key = i.section or "(no section)"
        groups.setdefault(key, []).append(i)

    for section, group in groups.items():
        print(f"\n{section}")
        print("─" * 56)
        for i in group:
            status = i.status(today)
            d = i.days_until_due(today)
            if status == "overdue":
                marker = f"OVERDUE {-d}d"
            elif status == "due-soon":
                marker = f"due in {d}d" if d > 0 else "due today"
            else:
                marker = f"ok ({d}d)"
            area = f"  {i.area}" if i.area else ""
            print(f"  {marker:<14}  {i.slug}  every:{i.every_str()}  last:{i.last_done}{area}")
    print()
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    new_date = args.date or date.today().isoformat()
    try:
        new_date = date.fromisoformat(new_date).isoformat()
    except ValueError:
        print(f"ERROR: invalid date '{new_date}', expected YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        changed = update_last_done(args.slug, new_date)
    except (ValueError, OverflowError) as exc:
        print(f"ERROR: invalid completion or next due date: {exc}", file=sys.stderr)
        return 2
    item = find_by_slug(args.slug) if changed else None
    if item is None:
        print(f"ERROR: no recurring item with slug '{args.slug}' after completion", file=sys.stderr)
        return 2
    print(f"✓ {args.slug}  last-done:{new_date}  next:{item.next_due().isoformat()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/recurring.py",
        description="Manage recurring obligations (re-emerging tasks).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List recurring items by status.")
    p_list.add_argument("--all", action="store_true", help="Include satisfied items.")
    p_list.add_argument("--area", help="Filter by area tag, e.g. #health")
    p_list.add_argument("--json", action="store_true", help="JSON output.")
    p_list.set_defaults(func=cmd_list)

    p_done = sub.add_parser("done", help="Mark a recurring item as completed.")
    p_done.add_argument("slug", help="The slug of the recurring item.")
    p_done.add_argument("date", nargs="?", help="Completion date (default: today).")
    p_done.set_defaults(func=cmd_done)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
