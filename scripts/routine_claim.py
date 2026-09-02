#!/usr/bin/env python3
"""Write canonical local-routine claims and decide whether a cycle may run."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cron_spec  # noqa: E402

SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VALID_STATUSES = {
    "running",
    "completed",
    "failed",
    "completion-uncertain",
    "deferred",
    "retry-approved",
}


def validate_cycle_id(value: str) -> str:
    """Return a canonical calendar cycle or reject malformed dates."""
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("cycle must use a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError("cycle must use canonical YYYY-MM-DD form")
    return value


def _claim_path(routine: str, cycle: str) -> Path:
    if not SAFE_COMPONENT.fullmatch(routine) or not SAFE_COMPONENT.fullmatch(cycle):
        raise ValueError("unsafe routine or cycle identifier")
    raw_ov = os.environ.get("OV", "")
    if not raw_ov:
        raise ValueError("OV is not set")
    vault = Path(raw_ov).expanduser().resolve()
    return vault / "_meta" / "routine_runs" / routine / f"{cycle}.toml"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def validate_claim(
    claim: dict[str, object],
    *,
    routine: str,
    cycle: str,
    allow_legacy_owner_generation: bool = False,
) -> str:
    """Validate fields shared by every canonical claim producer and consumer."""
    if claim.get("routine") != routine or claim.get("cycle_id") != cycle:
        raise ValueError("claim identity does not match its destination")
    status = claim.get("status")
    if status not in VALID_STATUSES:
        raise ValueError("claim has an invalid status")
    owner_generation = claim.get("owner_generation")
    if (
        allow_legacy_owner_generation
        and isinstance(owner_generation, str)
        and re.fullmatch(r"[0-9]+", owner_generation)
    ):
        owner_generation = int(owner_generation)
        claim["owner_generation"] = owner_generation
    if owner_generation is not None and (
        isinstance(owner_generation, bool)
        or not isinstance(owner_generation, int)
        or owner_generation < 0
    ):
        raise ValueError("owner_generation must be a non-negative integer")
    assert isinstance(status, str)
    return status


def write_claim(routine: str, cycle: str, content: str) -> Path:
    path = _claim_path(routine, cycle)
    try:
        claim = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"claim is invalid TOML: {exc}") from exc
    validate_claim(claim, routine=routine, cycle=cycle)
    retry_after = claim.get("retry_after_epoch")
    if retry_after is not None and (
        isinstance(retry_after, bool)
        or not isinstance(retry_after, int)
        or retry_after < 0
    ):
        raise ValueError("retry_after_epoch must be a non-negative integer")
    if not content.endswith("\n"):
        content += "\n"
    _atomic_write(path, content)
    return path


def schedule_decision(
    routine: str,
    cycle: str,
    *,
    now_epoch: int | None = None,
) -> dict[str, object]:
    """Return whether a scheduled invocation should attempt this cycle."""
    path = _claim_path(routine, cycle)
    if not path.exists():
        return {
            "action": "run",
            "reason": "claim-absent",
            "cycle_id": cycle,
        }
    try:
        claim = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"claim is unreadable or invalid TOML: {exc}") from exc
    status = validate_claim(
        claim,
        routine=routine,
        cycle=cycle,
        allow_legacy_owner_generation=True,
    )

    if status in {"completed", "running", "failed", "completion-uncertain"}:
        return {
            "action": "skip",
            "reason": f"claim-{status}",
            "cycle_id": cycle,
            "status": status,
        }
    if status == "retry-approved":
        return {
            "action": "run",
            "reason": "retry-approved",
            "cycle_id": cycle,
            "status": status,
        }

    retry_after = claim.get("retry_after_epoch", 0)
    if (
        isinstance(retry_after, bool)
        or not isinstance(retry_after, int)
        or retry_after < 0
    ):
        raise ValueError("deferred claim has invalid retry_after_epoch")
    now_epoch = int(time.time()) if now_epoch is None else now_epoch
    if now_epoch < retry_after:
        return {
            "action": "skip",
            "reason": "deferred-retry-not-due",
            "cycle_id": cycle,
            "status": status,
            "retry_after_epoch": retry_after,
            "seconds_remaining": retry_after - now_epoch,
        }
    return {
        "action": "run",
        "reason": "deferred-retry-due",
        "cycle_id": cycle,
        "status": status,
        "retry_after_epoch": retry_after,
    }


MAX_CATCHUP_DAYS = 7


def _declared_cron(routine: str) -> str:
    """The routine's declared cron from routine_watch.toml, or "" if none."""
    raw_ov = os.environ.get("OV", "")
    if not raw_ov:
        raise ValueError("OV is not set")
    path = Path(raw_ov).expanduser().resolve() / "_meta" / "routine_watch.toml"
    if not path.is_file():
        return ""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    for row in document.get("routine", []):
        if isinstance(row, dict) and row.get("name") == routine:
            cron = row.get("cron")
            return cron if isinstance(cron, str) else ""
    return ""


def _claim_status(routine: str, cycle: str) -> str | None:
    """Validated status of one claim, or None when no claim exists."""
    path = _claim_path(routine, cycle)
    if not path.exists():
        return None
    try:
        claim = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"claim for {cycle} is unreadable or invalid TOML: {exc}") from exc
    return validate_claim(
        claim,
        routine=routine,
        cycle=cycle,
        allow_legacy_owner_generation=True,
    )


def _select_by_cron(routine: str, cron: str, now: datetime) -> dict[str, object]:
    """Pick the cycle a cron-scheduled routine owes, from an arbitrary invocation.

    This is what lets a plist fire hourly without turning a weekly routine into
    a daily one. The plist becomes a recovery mechanism and the declared cron
    becomes the schedule, which is the split that was missing: before this,
    `routine_runner.sh` took `date +%Y-%m-%d` as the cycle for every routine
    except autoevo-nightly, so the plist *was* the schedule and an hourly plist
    would have meant a daily run.

    Selection only decides *which* cycle this invocation is for. Whether to act
    on it stays with `schedule_decision`, which owns claim-state policy; a
    failed cycle is still returned here and still refused there, because
    same-cycle retry after effects needs a human.
    """
    if not cron_spec.is_evaluable(cron):
        # Fail open. A malformed or non-literal schedule is a declaration bug,
        # and the wrong response to it is a routine that silently stops running.
        return {
            "action": "run",
            "reason": "current-cycle-unevaluable-cron",
            "cycle_id": now.date().isoformat(),
        }
    window = cron_spec.estimate_cadence_days(cron) or 1
    window = max(1, min(window, MAX_CATCHUP_DAYS))
    today = now.date()
    due = cron_spec.scheduled_dates(cron, today - timedelta(days=window), now)
    if not due:
        return {
            "action": "skip",
            "reason": "no-scheduled-occurrence-due",
            "cycle_id": today.isoformat(),
        }

    latest = due[-1]
    if latest == today:
        return {
            "action": "run",
            "reason": "current-cycle",
            "cycle_id": today.isoformat(),
        }

    cycle = latest.isoformat()
    status = _claim_status(routine, cycle)
    if status is None:
        return {"action": "run", "reason": "missed-scheduled-cycle", "cycle_id": cycle}
    if status == "completed":
        return {
            "action": "skip",
            "reason": "latest-scheduled-cycle-completed",
            "cycle_id": cycle,
            "status": status,
        }
    return {
        "action": "run",
        "reason": "scheduled-cycle-unresolved",
        "cycle_id": cycle,
        "status": status,
    }


def select_scheduled_cycle(
    routine: str,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Select the cycle for a calendar, wake, login, or reload invocation."""
    current = datetime.now().astimezone() if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("scheduled cycle selection requires a timezone-aware time")

    today = current.date()
    if routine != "autoevo-nightly":
        # autoevo-nightly keeps its bespoke pre-05:00 rule below. It is the one
        # routine whose hourly plist is already in production and verified, and
        # folding it into the generic path in the same change would put the
        # only working case at risk to remove a small duplication.
        cron = _declared_cron(routine)
        if not cron:
            return {
                "action": "run",
                "reason": "current-cycle-no-declared-cron",
                "cycle_id": today.isoformat(),
            }
        return _select_by_cron(routine, cron, current)

    if current.hour >= 5:
        return {
            "action": "run",
            "reason": "primary-or-missed-current-cycle",
            "cycle_id": today.isoformat(),
        }

    previous = (today - timedelta(days=1)).isoformat()
    path = _claim_path(routine, previous)
    if not path.exists():
        return {
            "action": "run",
            "reason": "missed-previous-cycle",
            "cycle_id": previous,
        }
    try:
        claim = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(
            f"previous claim is unreadable or invalid TOML: {exc}"
        ) from exc
    status = validate_claim(
        claim,
        routine=routine,
        cycle=previous,
        allow_legacy_owner_generation=True,
    )
    if status == "completed":
        return {
            "action": "skip",
            "reason": "previous-cycle-completed-before-primary",
            "cycle_id": previous,
            "status": status,
        }
    return {
        "action": "run",
        "reason": "previous-cycle-unresolved",
        "cycle_id": previous,
        "status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("routine")
    parser.add_argument("--cycle")
    parser.add_argument("--schedule-decision", action="store_true")
    parser.add_argument("--select-cycle", action="store_true")
    parser.add_argument("--validate-cycle")
    parser.add_argument(
        "--now",
        help="timezone-aware ISO time used only for deterministic cycle selection",
    )
    args = parser.parse_args()
    try:
        if args.validate_cycle is not None:
            if args.cycle or args.schedule_decision or args.select_cycle or args.now:
                raise ValueError(
                    "--validate-cycle cannot be combined with other cycle actions"
                )
            print(validate_cycle_id(args.validate_cycle))
            return 0
        if args.select_cycle:
            if args.cycle or args.schedule_decision:
                raise ValueError("--select-cycle cannot be combined with cycle actions")
            selected_at = datetime.fromisoformat(args.now) if args.now else None
            print(json.dumps(select_scheduled_cycle(args.routine, now=selected_at)))
            return 0
        if not args.cycle:
            raise ValueError("--cycle is required unless --select-cycle is used")
        if args.schedule_decision:
            print(json.dumps(schedule_decision(args.routine, args.cycle)))
            return 0
        path = write_claim(args.routine, args.cycle, sys.stdin.read())
    except (OSError, ValueError) as exc:
        operation = (
            "cycle selection" if args.select_cycle else "canonical claim operation"
        )
        print(f"ERROR: {operation} failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"written": True, "claim": str(path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
