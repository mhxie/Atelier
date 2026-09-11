#!/usr/bin/env python3
"""Serve local Atelier routines as self-hosted Prefect deployments."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

from prefect import flow, runtime, serve, task
from prefect.client.schemas.objects import ConcurrencyLimitConfig
from prefect.deployments import run_deployment
from prefect.schedules import Cron
from prefect.states import Failed

sys.path.insert(0, str(Path(__file__).resolve().parent))

import routine_adapter as adapter  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_RETRIES = 2
PREFLIGHT_RETRY_DELAY_SECONDS = [5, 30]


@task(
    name="validate routine boundary",
    retries=PREFLIGHT_RETRIES,
    retry_delay_seconds=PREFLIGHT_RETRY_DELAY_SECONDS,
    persist_result=False,
)
def prepare_model_task(routine: str) -> dict[str, Any]:
    """Retry only the preparation that cannot produce routine effects."""
    return adapter.prepare_model(routine)


@task(name="run headless Codex", retries=0, persist_result=False)
def model_attempt_task(payload: dict[str, Any], cycle: str, flow_run_id: str) -> dict[str, Any]:
    """One attempt only: after Codex starts, effects may be ambiguous."""
    return adapter.execute_model(payload, cycle=cycle, flow_run_id=flow_run_id)


@task(name="run deterministic job", retries=0, persist_result=False)
def process_attempt_task(job: str, source: str) -> None:
    adapter.execute_process_job(job, source)


def _flow_run_id() -> str:
    return str(runtime.flow_run.id or "direct")


def scheduled_cycle(timezone: str, explicit: str | None = None) -> str:
    if explicit:
        if not adapter.SAFE_CYCLE.fullmatch(explicit):
            raise adapter.ConfigurationError("cycle must use YYYY-MM-DD")
        return explicit
    scheduled = runtime.flow_run.scheduled_start_time
    when = scheduled if isinstance(scheduled, datetime) else datetime.now().astimezone()
    return when.astimezone(ZoneInfo(timezone)).date().isoformat()


@flow(name="atelier-model-routine", retries=0, persist_result=False, log_prints=True)
def model_routine_flow(routine: str, cycle: str | None = None) -> dict[str, Any] | Any:
    payload = prepare_model_task(routine)
    spec = adapter.ModelSpec.from_payload(payload)
    selected_cycle = scheduled_cycle(spec.schedule.timezone, cycle)
    try:
        return model_attempt_task.with_options(
            timeout_seconds=spec.profile_values["timeout_seconds"] + 60
        )(payload, selected_cycle, _flow_run_id())
    except adapter.DeferredRun as exc:
        # The next cron occurrence, not an in-process loop, owns reconsideration.
        return Failed(name="Deferred", message=str(exc))


@flow(name="atelier-process-routine", retries=0, persist_result=False, log_prints=True)
def process_routine_flow(job: str, source: str) -> None:
    spec = adapter.resolve_process(job, source)
    retries = spec.schedule.retries if spec.retry_safe else 0
    process_attempt_task.with_options(
        retries=retries,
        retry_delay_seconds=spec.schedule.retry_delay_seconds,
        timeout_seconds=spec.timeout_seconds + 30,
    )(job, source)


def deployments(*, root: Path = ROOT, environ: dict[str, str] | None = None) -> list[Any]:
    models, processes = adapter.load_specs(root=root, environ=environ)
    result = []
    concurrency = ConcurrencyLimitConfig(limit=1, collision_strategy="ENQUEUE")
    for spec in models:
        schedules = [
            Cron(expression, timezone=spec.schedule.timezone, parameters={"routine": spec.schedule.name})
            for expression in spec.schedule.cron
        ]
        result.append(
            model_routine_flow.to_deployment(
                name=spec.schedule.name,
                schedules=schedules,
                parameters={"routine": spec.schedule.name},
                concurrency_limit=concurrency,
                tags=["atelier", "model-routine"],
                description="Fixed headless-Codex local Atelier routine.",
            )
        )
    for spec in processes:
        parameters = {"job": spec.schedule.name, "source": spec.schedule.source}
        schedules = [
            Cron(expression, timezone=spec.schedule.timezone, parameters=parameters)
            for expression in spec.schedule.cron
        ]
        result.append(
            process_routine_flow.to_deployment(
                name=spec.schedule.name,
                schedules=schedules,
                parameters=parameters,
                concurrency_limit=concurrency,
                tags=["atelier", "deterministic-routine"],
                description="Deterministic local Atelier maintenance job.",
            )
        )
    return result


def validation_payload(*, root: Path = ROOT, environ: dict[str, str] | None = None) -> dict[str, Any]:
    models, processes = adapter.load_specs(root=root, environ=environ)
    for spec in [*models, *processes]:
        for expression in spec.schedule.cron:
            Cron(expression, timezone=spec.schedule.timezone)
    return {
        "valid": True,
        "engine": "prefect",
        "runtime": "codex",
        "global_concurrency": 1,
        "models": [
            {
                "name": spec.schedule.name,
                "cron": list(spec.schedule.cron),
                "timezone": spec.schedule.timezone,
                "profile": spec.profile,
                "adapter": spec.adapter,
            }
            for spec in models
        ],
        "processes": [
            {
                "name": spec.schedule.name,
                "cron": list(spec.schedule.cron),
                "timezone": spec.schedule.timezone,
                "source": spec.schedule.source,
                "retries": spec.schedule.retries if spec.retry_safe else 0,
            }
            for spec in processes
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate deployment declarations without an API call")
    validate_parser.add_argument("--json", action="store_true")
    subparsers.add_parser("serve", help="register and serve all local deployments")
    run_parser = subparsers.add_parser("run", help="run one model flow through the configured Prefect API")
    run_parser.add_argument("routine")
    run_parser.add_argument("--cycle", help="reviewed manual cycle override (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    try:
        if args.action == "validate":
            payload = validation_payload()
            print(json.dumps(payload, indent=2 if args.json else None, sort_keys=True))
            return 0
        with adapter.local_prefect_settings():
            if args.action == "run":
                if args.cycle:
                    adapter.validate_cycle_id(args.cycle)
                flow_run = run_deployment(
                    f"{model_routine_flow.name}/{args.routine}",
                    parameters={"routine": args.routine, "cycle": args.cycle},
                    as_subflow=False,
                )
                state = flow_run.state
                if state is None:
                    raise adapter.ExecutionError("manual deployment run returned no Prefect state")
                print(
                    json.dumps(
                        {"id": str(flow_run.id), "state": state.name, "type": state.type.value, "message": state.message},
                        sort_keys=True,
                    )
                )
                return 0 if state.is_completed() else 1
            configured = deployments()
            # One Runner-wide slot preserves the Mac's previous single-job resource
            # envelope; each deployment also queues duplicate overlapping runs.
            serve(*configured, limit=1, pause_on_shutdown=False)
            return 0
    except (adapter.ConfigurationError, adapter.ExecutionError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
