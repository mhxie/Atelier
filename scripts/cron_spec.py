#!/usr/bin/env python3
"""cron_spec.py: Parse and evaluate the cron strings declared in routine_watch.toml.

Extracted from `cues.py` so `routine_claim.py` can share it. That direction
matters: `cues.py` imports `routine_claim`, so the dependency cannot run the
other way, and duplicating schedule evaluation is exactly the kind of thing that
drifts silently until two parts of the harness disagree about what day it is.

The strings are annotated rather than bare cron, because a routine's declared
schedule has to say which zone it means:

    "0 13 * * 1 UTC (Mon 6 AM PT)"
    "0 5 * * *"

Everything from ` UTC` or ` (` onward is annotation. The presence of ` UTC`
selects the schedule zone; otherwise the local zone applies. Only the five
standard fields are supported, and only literal integers for minute and hour:
these are declarations a person wrote for a routine that runs once, not a
general cron implementation, and a `*/7` in the hour field would mean the
declaration is wrong rather than that this parser is too narrow.

The day fields accept the standard forms: `*`, `n`, `a-b`, `*/n`, `a-b/n`, and
comma lists of those. Steps count from the field's own origin, so `*/3` in the
month field is Jan/Apr/Jul/Oct as cron means it, not Mar/Jun/Sep/Dec. A field
that does not parse makes the whole string unevaluable (`is_evaluable` is
False) rather than never due: `routine_claim` gates cycle selection on this,
and a typo must fail open into a run, not silently retire the routine.

Day-of-week uses cron's convention where 0 and 7 are both Sunday; `cron_dow`
below converts from `date.weekday()`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

__all__ = [
    "cron_fields",
    "cron_field_matches",
    "field_values",
    "is_evaluable",
    "cron_dow",
    "estimate_cadence_days",
    "matches_date",
    "scheduled_dates",
]


def cron_fields(cron: str) -> tuple[str, str, str, str, str] | None:
    """Extract the five scheduling fields from an annotated cron string."""
    cron_clean = re.split(r"\s+UTC\b|\s+\(", cron, maxsplit=1)[0].strip()
    parts = cron_clean.split()
    if len(parts) < 5:
        return None
    return tuple(parts[:5])  # type: ignore[return-value]


# Inclusive bounds per field, in cron's own conventions. Day-of-week runs to 7
# so `7` (Sunday, again) parses; `_dow_matches` folds it onto 0.
_DOM_BOUNDS = (1, 31)
_MONTH_BOUNDS = (1, 12)
_DOW_BOUNDS = (0, 7)


def field_values(field: str, lo: int, hi: int) -> set[int] | None:
    """Expand one cron field into the integers it selects, or None if invalid."""
    values: set[int] = set()
    for item in field.split(","):
        step = 1
        if "/" in item:
            item, step_text = item.split("/", 1)
            try:
                step = int(step_text)
            except ValueError:
                return None
            if step <= 0:
                return None
        if item == "*":
            first, last = lo, hi
        elif "-" in item:
            first_text, last_text = item.split("-", 1)
            try:
                first, last = int(first_text), int(last_text)
            except ValueError:
                return None
        else:
            try:
                first = int(item)
            except ValueError:
                return None
            # `n/step` (Vixie extension) means n through the field maximum.
            last = hi if step != 1 else first
        if first < lo or last > hi or first > last:
            return None
        values.update(range(first, last + 1, step))
    return values


def cron_field_matches(value: int, field: str, *, one_based_step: bool = False) -> bool:
    """True when `value` is selected by `field`; False for an unparseable field.

    `one_based_step` says the field counts from 1 (day-of-month, month), which
    is where `*/n` starts. Prefer `field_values` with explicit bounds when the
    field is known; this keeps the older signature for callers that only match.
    """
    lo = 1 if one_based_step else 0
    values = field_values(field, lo, max(value, 59))
    return values is not None and value in values


def _dow_matches(day: date, field: str) -> bool:
    values = field_values(field, *_DOW_BOUNDS)
    if values is None:
        return False
    dow = cron_dow(day)
    return dow in values or (dow == 0 and 7 in values)


def _day_fields_valid(dom: str, month: str, dow: str) -> bool:
    return (
        field_values(dom, *_DOM_BOUNDS) is not None
        and field_values(month, *_MONTH_BOUNDS) is not None
        and field_values(dow, *_DOW_BOUNDS) is not None
    )


def is_evaluable(cron: str) -> bool:
    """True when this string can actually be turned into occurrence dates.

    Callers that gate execution on a cron must distinguish "not due" from "I
    could not read this". A typo in a declared schedule should not silently
    disable a routine, so anything unevaluable is meant to fail open.
    """
    fields = cron_fields(cron)
    if fields is None:
        return False
    minute_field, hour_field, dom, month, dow = fields
    try:
        int(minute_field)
        int(hour_field)
    except ValueError:
        return False
    return _day_fields_valid(dom, month, dow)


def cron_dow(day: date) -> int:
    """date.weekday() (Mon=0) to cron day-of-week (Sun=0)."""
    return (day.weekday() + 1) % 7


def _schedule_zone(cron: str, local_zone: timezone | None):
    return timezone.utc if " UTC" in cron else local_zone


def matches_date(cron: str, day: date) -> bool:
    """True when `day` is a scheduled day in the cron's own zone.

    Note this asks about the date in the *schedule's* zone, not the local one.
    A caller wanting local dates should use `scheduled_dates`, which converts.
    """
    fields = cron_fields(cron)
    if fields is None:
        return False
    _minute, _hour, dom, month, dow = fields
    return (
        cron_field_matches(day.day, dom, one_based_step=True)
        and cron_field_matches(day.month, month, one_based_step=True)
        and _dow_matches(day, dow)
    )


def scheduled_dates(cron: str, start: date, now: datetime) -> list[date]:
    """Local dates from `start` onward whose cron occurrence is already due."""
    fields = cron_fields(cron)
    if fields is None:
        return []
    minute_field, hour_field, dom, month, dow = fields
    try:
        minute = int(minute_field)
        hour = int(hour_field)
    except ValueError:
        return []

    local_now = now.astimezone()
    local_zone = local_now.tzinfo
    schedule_zone = _schedule_zone(cron, local_zone)
    schedule_now = local_now.astimezone(schedule_zone)
    earliest = start - timedelta(days=1)
    results: set[date] = set()
    cursor = schedule_now.date()

    while cursor >= earliest:
        candidate = datetime(
            cursor.year,
            cursor.month,
            cursor.day,
            hour,
            minute,
            tzinfo=schedule_zone,
        )
        if (
            candidate <= schedule_now
            and cron_field_matches(cursor.day, dom, one_based_step=True)
            and cron_field_matches(cursor.month, month, one_based_step=True)
            and _dow_matches(cursor, dow)
        ):
            local_date = candidate.astimezone(local_zone).date()
            if local_date >= start:
                results.add(local_date)
        cursor -= timedelta(days=1)

    return sorted(results)


def estimate_cadence_days(cron: str) -> int | None:
    """Estimate cadence in days from a cron-like expression.

    Handles common patterns from routine_watch.toml cron fields:
      "0 13 * * *"       -> daily (1)
      "0 10 * * 3"       -> weekly (7)
      "0 12 */3 * *"     -> every 3 days (3)
      "0 9 15 2,5,8,11 *" -> quarterly (~90)

    Deliberately approximate: it feeds staleness tolerance and hit-rate
    denominators, not cycle selection. `matches_date` is the exact one.
    """
    cron_clean = re.split(r"\s+UTC\b", cron)[0].strip()
    parts = cron_clean.split()
    if len(parts) < 5:
        return None

    _minute, _hour, dom, month, dow = parts[:5]

    if month != "*":
        if month.startswith("*/"):
            try:
                return 30 * int(month[2:])
            except ValueError:
                return None
        months = month.split(",")
        if len(months) >= 2:
            return 365 // len(months)
        return 30

    if dom.startswith("*/"):
        try:
            return int(dom[2:])
        except ValueError:
            return None

    if dom == "*" and dow == "*":
        return 1

    if dom == "*" and dow != "*":
        return 7

    return 30
