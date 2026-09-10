#!/usr/bin/env python3
"""Own Autoevo's cross-run per-scope quarantine state."""

from __future__ import annotations

import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import sys as _s
_s.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write as _atomic_write, retry_transient  # noqa: E402

QUARANTINE_EXPIRY_DAYS = 30
QUARANTINE_THRESHOLD = 3
VALID_OUTCOMES = {"envelope_returned", "forgetter_no_envelope"}


class QuarantineError(RuntimeError):
    """Quarantine state or supplied outcomes are invalid."""


def _load_active_entries(path: Path, today: date) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = tomllib.loads(
        retry_transient(
            lambda: path.read_text(encoding="utf-8"),
            what=f"read {path.name}",
        )
    )
    rows = data.get("quarantine", [])
    if not isinstance(rows, list):
        raise QuarantineError("quarantine state must contain an array of tables")

    active: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise QuarantineError("quarantine entry is not a table")
        scope = row.get("scope")
        first_failed = row.get("first_failed")
        reason = row.get("reason")
        expires_at = row.get("expires_at")
        failures = row.get("consecutive_failures")
        if (
            not isinstance(scope, str)
            or not scope
            or not isinstance(first_failed, str)
            or not isinstance(reason, str)
            or not isinstance(expires_at, str)
            or not isinstance(failures, int)
            or isinstance(failures, bool)
            or failures < 0
        ):
            raise QuarantineError(f"malformed quarantine entry for scope {scope!r}")
        try:
            first_failed_date = date.fromisoformat(first_failed)
            expires_at_date = date.fromisoformat(expires_at)
        except ValueError as exc:
            raise QuarantineError(
                f"quarantine entry has an invalid date for scope {scope!r}"
            ) from exc
        if reason != "forgetter_no_envelope":
            raise QuarantineError(
                f"quarantine entry has an invalid reason for scope {scope!r}"
            )
        if first_failed_date > today:
            raise QuarantineError(
                f"quarantine first failure is in the future for scope {scope!r}"
            )
        if expires_at_date <= first_failed_date:
            raise QuarantineError(
                f"quarantine expiry must follow first failure for scope {scope!r}"
            )
        if scope in active:
            raise QuarantineError(f"duplicate quarantine entry for scope {scope!r}")
        if expires_at_date > today:
            active[scope] = {
                "scope": scope,
                "first_failed": first_failed,
                "consecutive_failures": failures,
                "reason": reason,
                "expires_at": expires_at,
            }
    return active


def _render_state(entries: dict[str, dict[str, Any]]) -> str:
    import tomli_w

    text = tomli_w.dumps({"quarantine": [entries[scope] for scope in sorted(entries)]})
    tomllib.loads(text)
    return text


def active_scopes(*, state_path: Path, today: date) -> list[str]:
    """Return thresholded scopes active for the selected routine cycle date."""
    entries = _load_active_entries(state_path, today)
    return sorted(
        scope
        for scope, entry in entries.items()
        if int(entry["consecutive_failures"]) >= QUARANTINE_THRESHOLD
    )


def update_state(
    *,
    outcomes: dict[str, str],
    state_path: Path,
    today: date,
) -> int:
    """Apply one run's outcomes after pruning expired quarantine entries."""
    if not isinstance(outcomes, dict):
        raise QuarantineError("outcomes must be a mapping")
    entries = _load_active_entries(state_path, today)

    crossed_threshold = 0
    today_text = today.isoformat()
    expiry = (today + timedelta(days=QUARANTINE_EXPIRY_DAYS)).isoformat()
    for scope, outcome in outcomes.items():
        if not isinstance(scope, str) or not scope:
            raise QuarantineError("outcome scope must be a non-empty string")
        if not isinstance(outcome, str) or outcome not in VALID_OUTCOMES:
            raise QuarantineError(f"unknown outcome for {scope!r}: {outcome!r}")
        if outcome == "envelope_returned":
            entries.pop(scope, None)
            continue

        prior_count = int(entries.get(scope, {}).get("consecutive_failures", 0))
        entry = entries.setdefault(
            scope,
            {
                "scope": scope,
                "first_failed": today_text,
                "consecutive_failures": 0,
                "reason": "forgetter_no_envelope",
                "expires_at": expiry,
            },
        )
        entry["consecutive_failures"] = prior_count + 1
        if prior_count < QUARANTINE_THRESHOLD <= int(entry["consecutive_failures"]):
            crossed_threshold += 1

    _atomic_write(state_path, _render_state(entries))
    return crossed_threshold
