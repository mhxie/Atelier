#!/usr/bin/env python3
"""Evaluate annotated routine cron declarations with Prefect's cron engine."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from prefect._vendor.croniter import CroniterError, croniter
from prefect.server.schemas.schedules import CronSchedule

_SAMPLE_SIZE = 32
_SAMPLE_START = datetime(2024, 1, 1)
_NONDETERMINISTIC_FIELD = re.compile(
    r"[rh](?:\(\d+-\d+\))?(?:/\d+)?",
    re.IGNORECASE,
)


def _expression(declaration: str) -> str | None:
    """Return the five-field expression from a routine annotation, if valid."""
    expression = re.split(r"\s+UTC\b|\s+\(", declaration, maxsplit=1)[0].strip()
    fields = expression.split()
    if (
        len(fields) != 5
        or not all(field.isdecimal() for field in fields[:2])
        or any(
            _NONDETERMINISTIC_FIELD.fullmatch(part)
            for field in fields
            for part in field.split(",")
        )
        or not croniter.is_valid(expression)
    ):
        return None
    return expression


def schedule_zone(
    cron: str,
    local_zone: tzinfo | None,
    timezone_name: str | None = None,
) -> tzinfo:
    """Resolve the zone used by the deployed schedule declaration."""
    if timezone_name == "local":
        from tzlocal import get_localzone

        return get_localzone()
    if timezone_name is not None:
        return ZoneInfo(timezone_name)
    return timezone.utc if " UTC" in cron else (local_zone or timezone.utc)


def _zone_name(zone: tzinfo) -> str:
    if zone == timezone.utc:
        return "UTC"
    if name := getattr(zone, "key", None):
        return str(name)

    from tzlocal import get_localzone_name

    return get_localzone_name()


def scheduled_dates(
    cron: str,
    start: date,
    now: datetime,
    timezone_name: str | None = None,
) -> list[date]:
    """Return occurrences through ``now`` as explicit-zone or legacy dates."""
    expression = _expression(cron)
    if expression is None:
        return []

    local_now = now.astimezone()
    local_zone = local_now.tzinfo
    zone = schedule_zone(cron, local_zone, timezone_name)
    schedule_now = local_now.astimezone(zone)
    earliest = datetime.combine(start - timedelta(days=1), datetime.min.time(), zone)

    try:
        schedule = CronSchedule(cron=expression, timezone=_zone_name(zone))
        occurrences = schedule._get_dates_generator(start=earliest, end=schedule_now)
        dates = {
            occurrence.date()
            if timezone_name is not None
            else occurrence.astimezone(local_zone).date()
            for occurrence in occurrences
        }
    except CroniterError:
        return []
    return sorted(day for day in dates if day >= start)


def estimate_cadence_days(cron: str) -> int | None:
    """Estimate day cadence from representative Prefect cron occurrences."""
    expression = _expression(cron)
    if expression is None:
        return None

    try:
        iterator = croniter(expression, _SAMPLE_START - timedelta(minutes=1))
        dates = [iterator.get_next(datetime).date() for _ in range(_SAMPLE_SIZE)]
    except CroniterError:
        return None

    unique_dates = list(dict.fromkeys(dates))
    if len(unique_dates) < 2:
        return None
    average_gap = (unique_dates[-1] - unique_dates[0]).days / (len(unique_dates) - 1)
    return max(1, round(average_gap))
