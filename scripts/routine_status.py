#!/usr/bin/env python3
"""Bounded read-only status queries against the local Prefect API."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowFilter, FlowFilterName, FlowRunFilter, FlowRunFilterStartTime
from prefect.client.schemas.sorting import FlowRunSort
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId, LogFilterTimestamp
from prefect.client.schemas.sorting import LogSort

sys.path.insert(0, str(Path(__file__).resolve().parent))

import routine_adapter as adapter  # noqa: E402
from observability.usage import TOKEN_KEYS, from_log, number  # noqa: E402

# The Prefect REST API refuses a larger page with a 422, which surfaces as an
# opaque filter error. Keep the ceiling here so an over-large request is
# refused on its own terms before a client is ever opened.
MAX_LIMIT = 200
MAX_PAGES = 100


class StatusUnavailable(RuntimeError):
    """The local Prefect API could not answer a bounded status query."""


def _pages(read, errors, stage, **kwargs):
    """Retain earlier pages on failure; every report truncation is explicit."""
    for page in range(MAX_PAGES if errors is not None else 1):
        try:
            rows = read(**kwargs, offset=page * kwargs["limit"])
        except Exception as exc:
            if errors is None:
                raise
            errors.append({"stage": stage, "page": page, "error": type(exc).__name__})
            return
        yield from rows
        if len(rows) < kwargs["limit"]:
            return
    if errors is not None:
        errors.append({"stage": stage, "error": "page_cap", "max_pages": MAX_PAGES})


def _read(since, until, *, model_only=False, limit=MAX_LIMIT, usage=False, errors=None):
    if since.tzinfo is None:
        raise ValueError("since must have a timezone")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    names = ["atelier-model-routine"] if model_only else ["atelier-model-routine", "atelier-process-routine"]
    try:
        with adapter.local_prefect_settings():
            with get_client(sync_client=True, httpx_settings={"timeout": 2.0}) as client:
                runs = list(_pages(client.read_flow_runs, errors, "runs",
                    flow_filter=FlowFilter(name=FlowFilterName(any_=names)),
                    flow_run_filter=FlowRunFilter(start_time=FlowRunFilterStartTime(
                        after_=since, before_=until,
                    )),
                    sort=FlowRunSort.START_TIME_DESC,
                    limit=limit,
                ))
    except Exception as exc:  # noqa: BLE001 - local API/library boundary
        if errors is None:
            raise StatusUnavailable(str(exc)) from exc
        errors.append({"stage": "runs", "error": type(exc).__name__})
        runs = []
    measured = {}
    if usage and runs:
        try:
            with adapter.local_prefect_settings():
                with get_client(sync_client=True, httpx_settings={"timeout": 2.0}) as client:
                    logs = _pages(client.read_logs, errors, "observations",
                        log_filter=LogFilter(flow_run_id=LogFilterFlowRunId(any_=[run.id for run in runs]),
                                             timestamp=LogFilterTimestamp(before_=until) if errors is not None else None),
                        sort=LogSort.TIMESTAMP_DESC, limit=MAX_LIMIT,
                    )
                    for log in logs:
                        identity = str(log.flow_run_id)
                        record = from_log(log.message, identity)
                        if record and (errors is None or _valid_observation(record)):
                            measured.setdefault(identity, record)
        except Exception as exc:
            if errors is not None:
                errors.append({"stage": "observations", "error": type(exc).__name__})
    result = []
    for run in runs:
        state = run.state
        result.append(
            {
                "id": str(run.id),
                "routine": str(run.parameters.get("routine") or run.parameters.get("job") or ""),
                "state": state.type.value if state else "UNKNOWN",
                "state_name": state.name if state else "Unknown",
                "message": (state.message or "")[:300] if state else "",
                "start_time": run.start_time,
                "expected_start_time": run.expected_start_time,
                "end_time": run.end_time,
            }
        )
        if usage:
            result[-1]["observation"] = measured.get(str(run.id))
        if errors is not None:
            result[-1]["kind"] = "process" if "job" in run.parameters else "model"
    return result


def recent_runs(since: datetime, *, model_only: bool = False, limit: int = MAX_LIMIT,
                usage: bool = False) -> list[dict[str, Any]]:
    """Return bounded recent Atelier flow states, newest first."""
    return _read(since, datetime.now(timezone.utc), model_only=model_only, limit=limit, usage=usage)


def _valid_observation(record):
    values = record.get("usage")
    return (record.get("coverage") in ("complete", "partial", "unknown")
            and isinstance(values, dict)
            and all(key in values and (values[key] is None or number(values[key])) for key in TOKEN_KEYS)
            and (record["coverage"] != "complete" or all(number(values.get(key)) for key in ("input_tokens", "output_tokens")))
            and all(record.get(key) is None or number(record[key])
                    for key in ("duration_seconds", "cost_usd_estimate")))


def _summary(runs, until):
    states = dict.fromkeys(("COMPLETED", "FAILED", "DEFERRED", "RUNNING", "SCHEDULED"), 0)
    kinds = {"model": 0, "process": 0}
    coverage = dict.fromkeys(("complete", "partial", "unknown"), 0)
    wall, model = [], []
    samples = {key: [] for key in (*TOKEN_KEYS, "cost_usd_estimate")}
    for run in runs:
        state = "DEFERRED" if run["state_name"] == "Deferred" else run["state"]
        states[state] = states.get(state, 0) + 1
        kinds[run["kind"]] += 1
        start, end = run["start_time"], run["end_time"]
        if start and (end or state == "RUNNING"):
            wall.append(max(0, (min(end or until, until) - start).total_seconds()))
        if run["kind"] != "model":
            continue
        observation = run.get("observation") or {}
        coverage[observation.get("coverage", "unknown")] += 1
        if number(observation.get("duration_seconds")):
            model.append(observation["duration_seconds"])
        for key in samples:
            value = observation.get(key) if key == "cost_usd_estimate" else observation.get("usage", {}).get(key)
            if number(value):
                samples[key].append(value)
    def measured(values):
        return {"total": sum(values) if values else None, "measured_runs": len(values)}
    return {"runs": len(runs), "kinds": kinds, "states": states,
            "due_unstarted": sum(run["start_time"] is None for run in runs),
            "wall_seconds": {**measured(wall), "mean": statistics.mean(wall) if wall else None,
                             "median": statistics.median(wall) if wall else None,
                             "p95": sorted(wall)[math.ceil(len(wall) * .95) - 1] if wall else None},
            "model_seconds": measured(model), "usage_coverage": coverage,
            "tokens": {key: measured(samples[key]) for key in TOKEN_KEYS},
            "cost_usd_estimate": measured(samples["cost_usd_estimate"])}


def routine_report(since: datetime, until: datetime, *, model_only=False) -> dict:
    """Aggregate a fixed, start-or-due-time cohort; durations end at until."""
    if since.tzinfo is None or until.tzinfo is None or since >= until:
        raise ValueError("report requires an ordered timezone-aware window")
    errors = []
    runs = _read(since, until, model_only=model_only, usage=True, errors=errors)
    unique = {run["id"]: run for run in reversed(runs)
              if (when := run["start_time"] or run["expected_start_time"]) and since <= when <= until}
    routines, days = {}, {}
    for run in unique.values():
        routines.setdefault(run["routine"], []).append(run)
        day = (run["start_time"] or run["expected_start_time"]).astimezone().date().isoformat()
        days.setdefault(day, []).append(run)
    return {"window": {"since": since.isoformat(), "until": until.isoformat(),
                       "local_since": since.astimezone().isoformat(), "local_until": until.astimezone().isoformat(),
                       "local_timezone": str(until.astimezone().tzinfo), "cohort": "actual_start_or_unstarted_due"},
            "source": "local_prefect_flow_runs_and_observation_logs",
            "complete": not errors, "errors": errors, "page_size": MAX_LIMIT, "max_pages": MAX_PAGES,
            "overall": _summary(list(unique.values()), until),
            "by_routine": {key: _summary(value, until) for key, value in sorted(routines.items())},
            "by_day": {key: _summary(value, until) for key, value in sorted(days.items())}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=48, help="lookback window (default: 48)")
    parser.add_argument("--limit", type=int, default=50, help=f"maximum runs, at most {MAX_LIMIT} (default: 50)")
    parser.add_argument("--model-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--usage", action="store_true", help="Include bounded per-run observations; missing means unknown.")
    parser.add_argument("--report", action="store_true", help="Paginated aggregates, including usage; --limit applies only to legacy listing.")
    args = parser.parse_args(argv)
    if args.hours < 1:
        parser.error("--hours must be positive")
    if not 1 <= args.limit <= MAX_LIMIT:
        parser.error(f"--limit must be between 1 and {MAX_LIMIT}")
    if args.report:
        until = datetime.now(timezone.utc)
        report = routine_report(until - timedelta(hours=args.hours), until, model_only=args.model_only)
        print(json.dumps(report, sort_keys=True, indent=None if args.json else 2))
        return 0 if report["complete"] else 2
    try:
        runs = recent_runs(
            datetime.now(timezone.utc) - timedelta(hours=args.hours),
            model_only=args.model_only,
            limit=args.limit,
            usage=args.usage,
        )
    except StatusUnavailable as exc:
        print(f"ERROR: local Prefect status unavailable: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(runs, default=str, sort_keys=True))
        return 0
    if not runs:
        print("No Atelier Prefect runs in the selected window.")
        return 0
    for run in runs:
        started = run["start_time"] or run["expected_start_time"] or "-"
        print(f"{started}\t{run['routine']}\t{run['state_name']}\t{run['id']}")
        if args.usage:
            print("  observation: " + json.dumps(run["observation"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
