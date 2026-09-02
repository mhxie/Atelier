#!/usr/bin/env python3
"""Update autoevo quarantine state and place skip evidence in its audit section."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import sys as _s
_s.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import retry_transient  # noqa: E402

QUARANTINE_EXPIRY_DAYS = 30
QUARANTINE_THRESHOLD = 3
VALID_OUTCOMES = {"envelope_returned", "forgetter_no_envelope"}


class QuarantineError(RuntimeError):
    """Quarantine state or audit structure is unsafe to mutate."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    lines: list[str] = []
    for scope in sorted(entries):
        entry = entries[scope]
        lines.append("[[quarantine]]")
        for key in ("scope", "first_failed", "reason", "expires_at"):
            value = json.dumps(str(entry[key]), ensure_ascii=False)
            lines.append(f"{key} = {value}")
        lines.append(f"consecutive_failures = {int(entry['consecutive_failures'])}")
        lines.append("")
    return "\n".join(lines)


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
    outcomes_path: Path,
    state_path: Path,
    count_path: Path,
    today: date,
) -> int:
    """Apply one run's outcomes after pruning expired quarantine entries."""
    entries = _load_active_entries(state_path, today)
    if not outcomes_path.is_file():
        raise QuarantineError("required outcomes sidecar does not exist")
    outcomes: object = json.loads(outcomes_path.read_text(encoding="utf-8"))
    if not isinstance(outcomes, dict):
        raise QuarantineError("outcomes sidecar must be a JSON object")

    crossed_threshold = 0
    today_text = today.isoformat()
    expiry = (today + timedelta(days=QUARANTINE_EXPIRY_DAYS)).isoformat()
    for scope, outcome in outcomes.items():
        if not isinstance(scope, str) or not scope:
            raise QuarantineError("outcome scope must be a non-empty string")
        if outcome not in VALID_OUTCOMES:
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

    _atomic_write(count_path, f"{crossed_threshold}\n")
    _atomic_write(state_path, _render_state(entries))
    return crossed_threshold


def insert_skipped(*, audit_path: Path, skipped_path: Path) -> bool:
    """Insert generated quarantine lines into the latest audit Skipped section."""
    if not skipped_path.is_file():
        return False
    additions = [
        line.strip()
        for line in skipped_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not additions:
        return False
    if not audit_path.is_file():
        raise QuarantineError("audit file does not exist")

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    run_indexes = [
        index for index, line in enumerate(lines) if line.startswith("## Autoevo Run:")
    ]
    if not run_indexes:
        raise QuarantineError("audit file has no Autoevo Run section")
    run_start = run_indexes[-1]
    try:
        skipped_index = lines.index("### Skipped (reason)", run_start)
        errors_index = lines.index("### Errors", skipped_index + 1)
    except ValueError as exc:
        raise QuarantineError(
            "latest audit run is missing Skipped or Errors section"
        ) from exc

    existing = [
        line.strip()
        for line in lines[skipped_index + 1 : errors_index]
        if line.strip() and line.strip() != "- (none)"
    ]
    normalized = [line if line.startswith("- ") else f"- {line}" for line in additions]
    merged = list(existing)
    changed = False
    for line in normalized:
        if line not in merged:
            merged.append(line)
            changed = True
    if not changed:
        return False

    rendered = lines[: skipped_index + 1] + merged + [""] + lines[errors_index:]
    _atomic_write(audit_path, "\n".join(rendered).rstrip() + "\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    update = subparsers.add_parser("update")
    update.add_argument("--outcomes", type=Path, required=True)
    update.add_argument("--state", type=Path, required=True)
    update.add_argument("--count-file", type=Path, required=True)
    update.add_argument("--today", type=date.fromisoformat, default=date.today())

    active = subparsers.add_parser("active-scopes")
    active.add_argument("--state", type=Path, required=True)
    active.add_argument("--today", type=date.fromisoformat, required=True)

    insert = subparsers.add_parser("insert-skipped")
    insert.add_argument("--audit", type=Path, required=True)
    insert.add_argument("--skipped-lines", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "update":
            update_state(
                outcomes_path=args.outcomes,
                state_path=args.state,
                count_path=args.count_file,
                today=args.today,
            )
        elif args.command == "active-scopes":
            for scope in active_scopes(state_path=args.state, today=args.today):
                print(scope)
        else:
            insert_skipped(
                audit_path=args.audit,
                skipped_path=args.skipped_lines,
            )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
        QuarantineError,
    ) as exc:
        print(f"autoevo_quarantine: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
