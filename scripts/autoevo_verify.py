#!/usr/bin/env python3
"""Verify that one autoevo cycle completed a real decay sweep."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path

from _paths import retry_transient, tier
from _git import run_git  # noqa: E402
from autoevo_preflight import (  # noqa: E402
    PreflightError,
    _in_scope,
    _is_autoevo_state,
    _status_entries,
    autoevo_scope_prefixes,
)

SAFE_CYCLE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_HEADING = re.compile(r"(?m)^## Autoevo Run:")
SWEEP_HEADING = re.compile(r"(?m)^### Sweep coverage \((\d+)\)\s*$")
REPORT_HEADING = re.compile(r"(?m)^### Sweep reports \((\d+)\)\s*$")


class VerificationError(RuntimeError):
    """The selected cycle lacks authoritative completion evidence."""


def _vault(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    raw = os.environ.get("OV", "")
    if not raw:
        raise VerificationError("OV is not set")
    return Path(raw).expanduser().resolve()


def _tier_path(vault: Path, name: str, fixture_default: str) -> Path:
    """Resolve production tiers while keeping explicit test vaults isolated."""
    raw_ov = os.environ.get("OV", "")
    configured_vault = Path(raw_ov).expanduser().resolve() if raw_ov else None
    if configured_vault == vault:
        return tier(name).resolve()
    return (vault / fixture_default).resolve()


def _read_toml(path: Path) -> dict[str, object]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VerificationError(f"cannot read claim: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError("claim is not a TOML table")
    return value


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise VerificationError(f"claim omitted {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise VerificationError(f"claim has invalid {field}") from exc
    if parsed.tzinfo is None:
        raise VerificationError(f"claim {field} has no timezone")
    return parsed


def _output_path(vault: Path, raw: object, cycle: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise VerificationError("claim omitted output_file")
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise VerificationError("claim output_file is not a safe vault-relative path")
    path = (vault / relative).resolve()
    try:
        path.relative_to(vault)
    except ValueError as exc:
        raise VerificationError("claim output_file escapes the vault") from exc
    if path.parent != _tier_path(vault, "agent_findings", "agent-findings"):
        raise VerificationError("claim output_file is outside agent-findings")
    if not re.fullmatch(r"autoevo-applied-\d{4}-\d{2}-\d{2}\.md", path.name):
        raise VerificationError("claim output_file is not a canonical autoevo audit")
    if path.name != f"autoevo-applied-{cycle}.md":
        raise VerificationError("claim output_file date does not match its cycle")
    if not path.is_file() or path.stat().st_size == 0:
        raise VerificationError("claim output_file is absent or empty")
    return path


def _event_log_path(vault: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw:
        raise VerificationError("claim omitted event_log")
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise VerificationError("claim event_log is not a safe vault-relative path")
    path = (vault / relative).resolve()
    try:
        path.relative_to(vault)
    except ValueError as exc:
        raise VerificationError("claim event_log escapes the vault") from exc
    if path.parent != _tier_path(vault, "cache", "cache"):
        raise VerificationError("claim event_log is outside cache")
    if not re.fullmatch(
        r"autoevo-runner-\d{4}-\d{2}-\d{2}\.log\.[A-Za-z0-9]+",
        path.name,
    ):
        raise VerificationError("claim event_log is not a canonical runner journal")
    if not path.is_file() or path.stat().st_size == 0:
        raise VerificationError("claim event_log is absent or empty")
    return path


def _latest_run(text: str) -> str:
    matches = list(RUN_HEADING.finditer(text))
    if not matches:
        raise VerificationError("audit has no Autoevo Run section")
    return text[matches[-1].start() :]


def _section(run: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ms)^### {re.escape(heading)}(?: \([^\n]*\))?\s*$\n"
        rf"(.*?)(?=^### |\Z)"
    )
    match = pattern.search(run)
    if not match:
        raise VerificationError(f"audit omitted {heading}")
    return match.group(1).strip()


def _verify_audit(path: Path, minimum_sweeps: int) -> dict[str, object]:
    run = _latest_run(path.read_text(encoding="utf-8"))
    run_id_match = re.search(r"(?m)^Run ID: (\d{8}-\d{6})\s*$", run)
    if not run_id_match:
        raise VerificationError("latest audit run omitted Run ID")
    run_id = run_id_match.group(1)
    heading = SWEEP_HEADING.search(run)
    if not heading:
        raise VerificationError("latest audit run has no Sweep coverage section")
    declared = int(heading.group(1))
    coverage = _section(run, "Sweep coverage")
    coverage_entries: dict[str, str] = {}
    for line in coverage.splitlines():
        if not line.startswith("- ") or ": " not in line:
            raise VerificationError("Sweep coverage has a malformed entry")
        scope, outcome = line[2:].rsplit(": ", 1)
        if not scope or outcome not in {
            "envelope_returned",
            "forgetter_no_envelope",
        }:
            raise VerificationError("Sweep coverage has an invalid entry")
        if scope in coverage_entries:
            raise VerificationError("Sweep coverage repeats a scope")
        coverage_entries[scope] = outcome
    returned = sum(
        outcome == "envelope_returned" for outcome in coverage_entries.values()
    )
    failed = sum(
        outcome == "forgetter_no_envelope" for outcome in coverage_entries.values()
    )
    if declared != returned + failed:
        raise VerificationError("Sweep coverage count does not match its entries")
    if failed:
        raise VerificationError("one or more Forgetter sweeps returned no envelope")
    if returned < minimum_sweeps:
        raise VerificationError(
            f"only {returned} completed sweeps; expected at least {minimum_sweeps}"
        )

    report_heading = REPORT_HEADING.search(run)
    if not report_heading:
        raise VerificationError("latest audit run has no Sweep reports section")
    declared_reports = int(report_heading.group(1))
    reports_section = _section(run, "Sweep reports")
    reports: list[str] = []
    for line in reports_section.splitlines():
        if not line.startswith("- "):
            raise VerificationError("Sweep reports has a malformed entry")
        raw = line[2:]
        relative = Path(raw)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or not re.fullmatch(
                rf"decay-{re.escape(run_id)}-[^/]+\.md",
                relative.name,
            )
        ):
            raise VerificationError("Sweep reports has an unsafe or invalid path")
        if raw in reports:
            raise VerificationError("Sweep reports repeats a path")
        reports.append(raw)
    if declared_reports != len(reports) or len(reports) != returned:
        raise VerificationError(
            "Sweep reports count does not match returned sweep coverage"
        )

    lint = _section(run, "Lint")
    if "Not run:" in lint:
        raise VerificationError("latest audit does not prove lint ran")
    lint_counts: dict[str, int] = {}
    for severity in ("error", "warn", "info"):
        match = re.search(rf"(?i)\b{severity}:\s*(\d+)", lint)
        if not match:
            raise VerificationError(f"latest audit omitted lint count for {severity}")
        lint_counts[severity] = int(match.group(1))
    skipped = _section(run, "Skipped (reason)")
    if not re.fullmatch(r"- \(none\)", skipped):
        raise VerificationError("latest audit run contains skipped work")
    errors = _section(run, "Errors")
    if not re.fullmatch(r"- \(none\)", errors):
        raise VerificationError("latest audit run contains errors")
    return {
        "run_id": run_id,
        "coverage": coverage_entries,
        "reports": reports,
        "sweeps_completed": returned,
        "lint_counts": lint_counts,
        "lint": lint.splitlines(),
    }


def _verify_sidecars(
    vault: Path,
    run_id: str,
    coverage: dict[str, str],
    lint_counts: dict[str, int],
) -> dict[str, str]:
    cache = _tier_path(vault, "cache", "cache")
    outcomes_path = cache / f"autoevo-{run_id}-outcomes.json"
    lint_path = cache / f"autoevo-{run_id}-lint.json"
    try:
        outcomes = json.loads(outcomes_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read outcomes sidecar: {exc}") from exc
    if outcomes != coverage:
        raise VerificationError("audit Sweep coverage does not match outcomes sidecar")
    try:
        lint = json.loads(lint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read lint sidecar: {exc}") from exc
    if not isinstance(lint, dict) or lint.get("counts") != lint_counts:
        raise VerificationError("audit lint counts do not match lint sidecar")
    return {
        "outcomes": str(outcomes_path),
        "lint": str(lint_path),
    }


def _is_protected(path: str, protected: set[str]) -> bool:
    """Git collapses a wholly untracked directory into one `dir/` entry, so a
    plan-time entry ending in `/` covers every path beneath it."""
    return path in protected or any(
        entry.endswith("/") and path.startswith(entry) for entry in protected
    )


def leftover_paths(
    entries: list[tuple[str, str]], prefixes: list[str], protected: set[str]
) -> list[str]:
    """Dirty paths the bot, not the user, answers for after a cycle.

    Same split as the preflight: autoevo state under `_meta/autoevo_*.toml`
    is always the bot's; an in-scope content path is the bot's unless the
    plan recorded it as protected user dirt; every other path is the user's.
    """
    leftovers = {
        path
        for _code, path in entries
        if _is_autoevo_state(path)
        or (_in_scope(path, prefixes) and not _is_protected(path, protected))
    }
    return sorted(leftovers)


def _protected_paths(vault: Path, run_id: str) -> tuple[set[str], bool]:
    """(protected paths recorded by `plan`, whether the list existed)."""
    path = _tier_path(vault, "cache", "cache") / f"autoevo-{run_id}-protected.txt"
    try:
        text = retry_transient(
            lambda: path.read_text(encoding="utf-8"), what="protected list read"
        )
    except OSError:
        return set(), False
    return {line.strip() for line in text.splitlines() if line.strip()}, True


def _git_commit(vault: Path, output: Path, run_id: str) -> str:
    relative = output.relative_to(vault).as_posix()
    result = run_git(vault, "log", "-1", "--format=%H", "--", relative, timeout=30)
    commit = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise VerificationError("audit output has no Git commit")
    try:
        entries = _status_entries(vault)
    except PreflightError as exc:
        raise VerificationError("cannot inspect final vault worktree") from exc
    protected, listed = _protected_paths(vault, run_id)
    leftovers = leftover_paths(entries, autoevo_scope_prefixes(vault), protected)
    if leftovers:
        raise VerificationError(
            "bot-owned paths are dirty after the cycle"
            + ("" if listed else " (no protected list for this run)")
            + ": "
            + ", ".join(leftovers[:5])
        )
    return commit


def _verify_reports(
    vault: Path,
    output: Path,
    reports: list[str],
    audit_commit: str,
) -> list[str]:
    verified: list[str] = []
    for raw in reports:
        report = (vault / raw).resolve()
        try:
            report.relative_to(vault)
        except ValueError as exc:
            raise VerificationError("Sweep report escapes the vault") from exc
        if report.parent != output.parent:
            raise VerificationError("Sweep report is outside agent-findings")
        if not report.is_file() or report.stat().st_size == 0:
            raise VerificationError(f"Sweep report is absent or empty: {raw}")
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", raw],
            cwd=vault,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or result.stdout.strip() != audit_commit:
            raise VerificationError(
                f"Sweep report was not committed with the audit: {raw}"
            )
        verified.append(str(report))
    return verified


def _verify_wrapper_log(path: Path, cycle: str) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise VerificationError(f"cannot read wrapper log: {exc}") from exc
    claimed = list(
        re.finditer(
            rf"(?m)^\[[^\]]+\] claimed: .*"
            rf"/{re.escape(cycle)}\.toml\s*$",
            text,
        )
    )
    if not claimed:
        raise VerificationError("wrapper log has no claim for the cycle")
    segment = text[claimed[-1].start() :]
    required = (
        "deterministic autoevo preflight passed",
        "starting: runtime=codex command=/autoevo-nightly",
        "delivery validated: outcome=delivered",
        "finished: status=completed",
        "lock release:",
    )
    offsets = []
    for marker in required:
        offset = segment.find(marker)
        if offset < 0:
            raise VerificationError(f"wrapper log omitted: {marker}")
        offsets.append(offset)
    if offsets != sorted(offsets) or len(set(offsets)) != len(offsets):
        raise VerificationError("wrapper completion markers are out of order")
    return {"markers_verified": list(required)}


def verify_cycle(
    cycle: str,
    *,
    vault: Path | None = None,
    wrapper_log: Path | None = None,
    minimum_sweeps: int = 3,
    allow_pending_claim: bool = False,
) -> dict[str, object]:
    """Return authoritative evidence for one completed autoevo cycle."""
    if not SAFE_CYCLE.fullmatch(cycle):
        raise VerificationError("cycle must use YYYY-MM-DD")
    vault = _vault(vault)
    claim_path = vault / "_meta" / "routine_runs" / "autoevo-nightly" / f"{cycle}.toml"
    claim = _read_toml(claim_path)
    if claim.get("routine") != "autoevo-nightly" or claim.get("cycle_id") != cycle:
        raise VerificationError("claim identity does not match the cycle")
    claim_status = claim.get("status")
    claim_verification = claim.get("verification")
    if allow_pending_claim:
        if claim_status != "completion-uncertain" or claim_verification != "pending":
            raise VerificationError(
                "internal verification requires a pending completion-uncertain claim"
            )
    elif claim_status != "completed" or claim_verification != "passed":
        raise VerificationError(
            f"claim completion evidence is "
            f"{claim_status or 'absent'}/{claim_verification or 'absent'}"
        )
    if claim.get("outcome") != "delivered":
        raise VerificationError(
            f"claim outcome is {claim.get('outcome', 'absent')}, not delivered"
        )
    claimed_at = _parse_time(claim.get("claimed_at"), "claimed_at")
    completed_at = _parse_time(claim.get("completed_at"), "completed_at")
    if completed_at < claimed_at:
        raise VerificationError("claim completed_at precedes claimed_at")
    duration = claim.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise VerificationError("claim has invalid duration_seconds")

    event_log = _event_log_path(vault, claim.get("event_log"))
    if wrapper_log is None:
        wrapper_log = event_log
    elif wrapper_log.expanduser().resolve() != event_log:
        raise VerificationError("selected wrapper log does not match claim event_log")

    output = _output_path(vault, claim.get("output_file"), cycle)
    audit = _verify_audit(output, minimum_sweeps)
    sidecars = _verify_sidecars(
        vault,
        str(audit["run_id"]),
        dict(audit["coverage"]),
        dict(audit["lint_counts"]),
    )
    commit = _git_commit(vault, output, str(audit["run_id"]))
    reports = _verify_reports(
        vault,
        output,
        list(audit["reports"]),
        commit,
    )
    if not allow_pending_claim:
        verified_at = _parse_time(claim.get("verified_at"), "verified_at")
        if verified_at < completed_at:
            raise VerificationError("claim verified_at precedes completed_at")
        if claim.get("verified_sweeps") != audit["sweeps_completed"]:
            raise VerificationError("claim verified_sweeps does not match the audit")
        if claim.get("verification_commit") != commit:
            raise VerificationError("claim verification_commit does not match Git")
    log = _verify_wrapper_log(wrapper_log, cycle)
    return {
        "verified": True,
        "cycle_id": cycle,
        "claim": str(claim_path),
        "output_file": str(output),
        "duration_seconds": duration,
        "sweeps_completed": audit["sweeps_completed"],
        "sweep_reports": reports,
        "audit_commit": commit,
        "lint": audit["lint"],
        "outcomes_sidecar": sidecars["outcomes"],
        "lint_sidecar": sidecars["lint"],
        "event_log": str(event_log),
        "wrapper_markers": log["markers_verified"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", default=date.today().isoformat())
    parser.add_argument("--vault", type=Path)
    parser.add_argument(
        "--wrapper-log",
        type=Path,
        help="override the claim-owned runner journal (must be the same path)",
    )
    parser.add_argument("--minimum-sweeps", type=int, default=3)
    parser.add_argument("--allow-pending-claim", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.minimum_sweeps < 1:
        print("ERROR: --minimum-sweeps must be positive", file=sys.stderr)
        return 2
    try:
        payload = verify_cycle(
            args.cycle,
            vault=args.vault,
            wrapper_log=args.wrapper_log,
            minimum_sweeps=args.minimum_sweeps,
            allow_pending_claim=args.allow_pending_claim,
        )
    except VerificationError as exc:
        payload = {
            "verified": False,
            "cycle_id": args.cycle,
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # keep the one-JSON-object contract for headless callers
        payload = {"verified": False, "cycle_id": args.cycle, "error": f"{type(exc).__name__}: {exc}"[:300]}
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"ERROR: {payload['error']}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"verified autoevo cycle {args.cycle}: "
            f"{payload['sweeps_completed']} sweeps, "
            f"duration={payload['duration_seconds']}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
