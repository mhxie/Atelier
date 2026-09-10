#!/usr/bin/env python3
"""Bounded read-only status queries against the local Prefect API."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowFilter, FlowFilterName, FlowRunFilter, FlowRunFilterStartTime
from prefect.client.schemas.sorting import FlowRunSort

sys.path.insert(0, str(Path(__file__).resolve().parent))

import routine_adapter as adapter  # noqa: E402

# The Prefect REST API refuses a larger page with a 422, which surfaces as an
# opaque filter error. Keep the ceiling here so an over-large request is
# refused on its own terms before a client is ever opened.
MAX_LIMIT = 200


class StatusUnavailable(RuntimeError):
    """The local Prefect API could not answer a bounded status query."""


def recent_runs(since: datetime, *, model_only: bool = False, limit: int = MAX_LIMIT) -> list[dict[str, Any]]:
    """Return normalized recent Atelier flow states, newest first."""
    if since.tzinfo is None:
        raise ValueError("since must have a timezone")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    names = ["atelier-model-routine"] if model_only else ["atelier-model-routine", "atelier-process-routine"]
    try:
        with adapter.local_prefect_settings():
            with get_client(sync_client=True, httpx_settings={"timeout": 2.0}) as client:
                runs = client.read_flow_runs(
                    flow_filter=FlowFilter(name=FlowFilterName(any_=names)),
                    flow_run_filter=FlowRunFilter(start_time=FlowRunFilterStartTime(
                        after_=since, before_=datetime.now(timezone.utc),
                    )),
                    sort=FlowRunSort.START_TIME_DESC,
                    limit=limit,
                )
    except Exception as exc:  # noqa: BLE001 - local API/library boundary
        raise StatusUnavailable(str(exc)) from exc
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
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=48, help="lookback window (default: 48)")
    parser.add_argument("--limit", type=int, default=50, help=f"maximum runs, at most {MAX_LIMIT} (default: 50)")
    parser.add_argument("--model-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.hours < 1:
        parser.error("--hours must be positive")
    if not 1 <= args.limit <= MAX_LIMIT:
        parser.error(f"--limit must be between 1 and {MAX_LIMIT}")
    try:
        runs = recent_runs(
            datetime.now(timezone.utc) - timedelta(hours=args.hours),
            model_only=args.model_only,
            limit=args.limit,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
