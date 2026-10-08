#!/usr/bin/env python3
"""Quiet-by-default session cues: key, severity, command path, message as TSV.

Missing configuration and individual check failures never block session start.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

# Allow running as `uv run scripts/cues.py` from atelier root.
sys.path.insert(0, str(Path(__file__).parent))
from _paths import date_in_text, raw_store, tier, tier_files, tier_segments, vault_root  # type: ignore[import-not-found]  # noqa: E402
import cron_spec  # noqa: E402
import intent_coverage  # noqa: E402
import autoevo_preflight  # noqa: E402
import autoevo_verify  # noqa: E402
import routine_status  # noqa: E402
import routine_receipts  # noqa: E402


@dataclass
class Cue:
    key: str
    severity: str  # "hard" | "soft"
    command_path: str  # relative path to the command file to route into on Yes
    message: str  # user-facing Chinese prompt
    count: int | None = None  # queue size, when the cue backs a /triage lane
    items: list[str] | None = None  # that queue's entries, for the lane's batch


def _resolve_output_runtime(requested: str) -> str:
    """Resolve which native command syntax user-facing cue text should use."""
    if requested in {"claude", "codex"}:
        return requested
    active = os.environ.get("ATELIER_ACTIVE_RUNTIME")
    if active in {"claude", "codex"}:
        return active
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    try:
        from atelier_runtime import load_registry, resolve_runtime

        runtime, _ = resolve_runtime(load_registry())
        return runtime
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as exc:
        # Cue rendering stays fail-open (wrong syntax beats a dead session
        # start), but a broken registry should not be silent.
        print(f"# warning: runtime resolution failed ({exc!r}); using codex", file=sys.stderr)
        return "codex"


def _format_runtime_message(message: str, runtime: str) -> str:
    """Render registered workflow references in the active runtime's syntax."""
    if runtime != "codex":
        return message
    try:
        from registries import load_skills

        skills = load_skills()
    except Exception:  # noqa: BLE001  (cosmetic rendering stays fail-open)
        return message
    for name, entry in sorted(skills.items(), key=lambda item: -len(item[0])):
        if not isinstance(entry, dict):
            continue
        replacement = (
            f"`${name}`" if entry.get("user_facing", True) is not False else f"`{name}`"
        )
        message = message.replace(f"`/{name}`", replacement)
    return message



def _meta_dir(ov: Path) -> Path:
    """Operational-state root under the caller's vault, registry-renamable."""
    return ov / tier_segments().get("meta", "_meta")


def _routine_registry(ov: Path) -> Path:
    return ov / tier_segments().get("private_routines", "_tools/routines") / "registry.toml"


def _touch_session_lock(verbose: bool, context: str) -> None:
    """SessionStart refresh; the lock itself is owned by autoevo_preflight."""
    try:
        touched = autoevo_preflight.touch_session_lock()
    except OSError as exc:
        if verbose:
            print(f"# debug: session-lock touch failed: {exc!r}", file=sys.stderr)
        return
    if not touched and verbose:
        print(
            f"# debug: {context} lock touch skipped (ATELIER_SKIP_LOCK_TOUCH set)",
            file=sys.stderr,
        )


# --- individual checks ----------------------------------------------------


def check_weekly(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface weekly review cadence debt."""
    if not tier("reflections").is_dir():
        return None, "reflections dir missing; skip weekly cue"

    weeklies = tier_files("reflections", "*-weekly.md")
    if not weeklies:
        return (
            Cue(
                key="weekly",
                severity="hard",
                command_path="skills/weekly/SKILL.md",
                message=(
                    "还没跑过 weekly. 这周已经积累了 Apple Health / 信号 / "
                    "健康 cadence checks 没补齐. 建议先跑 `/weekly`. 现在跑吗?"
                ),
            ),
            "no weekly found; hard floor",
        )

    latest = weeklies[-1]
    try:
        latest_date = datetime.strptime(latest.name[:10], "%Y-%m-%d").date()
    except ValueError:
        return None, f"could not parse date from {latest.name}; skip"

    days_since = (today - latest_date).days

    if days_since > 10:
        return (
            Cue(
                key="weekly",
                severity="hard",
                command_path="skills/weekly/SKILL.md",
                message=(
                    f"上次 weekly 是 {days_since} 天前. 这周已经积累了 Apple Health / "
                    f"信号 / 健康 cadence checks 没补齐. 建议先跑 `/weekly`. 现在跑吗?"
                ),
            ),
            f"days_since={days_since} > 10; hard floor",
        )

    weekday = today.weekday()  # Mon=0, Sun=6
    if days_since > 6 and weekday in (6, 0):  # Sun or Mon
        return (
            Cue(
                key="weekly",
                severity="soft",
                command_path="skills/weekly/SKILL.md",
                message=(
                    f"提示: 上次 weekly 是 {days_since} 天前. "
                    f"想现在跑 `/weekly` 把这周补齐吗?"
                ),
            ),
            f"days_since={days_since}, weekday={weekday}; soft cue",
        )

    return None, f"days_since={days_since}, weekday={weekday}; fresh"



def check_reflect_intake(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface intake debt, excluding Reflect hubs and paired audio transcripts."""
    notes = tier("notes")
    if not notes.is_dir():
        return None, "notes/ missing; skip"
    memos = tier("audio_memos")
    kept = {"links", "audio-memos"} | ({p.stem for p in memos.iterdir()} if memos.is_dir() else set())
    pending = [p for p in notes.glob("*.md") if p.stem not in kept]
    if not pending:
        return None, "notes/ empty; fresh"
    n = len(pending)
    oldest_age_days = (today - date.fromtimestamp(min(p.stat().st_mtime for p in pending))).days
    hard = n >= 5 or oldest_age_days > 14
    return (
        Cue(
            key="reflect_intake",
            severity="hard" if hard else "soft",
            command_path="skills/triage/SKILL.md",
            message=f"Reflect 新建了 {n} 条笔记待归档 (最老 {oldest_age_days} 天). 想现在跑 `/triage` 归位吗?",
            count=n,
            items=sorted(p.name for p in pending),
        ),
        f"n={n} oldest_age={oldest_age_days}; {'hard floor' if hard else 'soft cue'}",
    )


def check_recurring(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface due-soon and overdue obligations from the recurring ledger."""
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from recurring import parse_file  # type: ignore[import-not-found]
    except ImportError as exc:
        return None, f"recurring import failed: {exc!r}"

    items = parse_file()
    if not items:
        return None, "no recurring items defined"

    overdue = [i for i in items if i.status(today) == "overdue"]
    due_soon = [i for i in items if i.status(today) == "due-soon"]
    if not overdue and not due_soon:
        return None, f"all {len(items)} recurring items satisfied"

    parts = []
    worst_days = 0
    if overdue:
        overdue.sort(key=lambda i: i.days_until_due(today))
        top = overdue[0]
        worst_days = -top.days_until_due(today)
        parts.append(f"{len(overdue)} overdue (worst: {top.slug} -{worst_days}d)")
    if due_soon:
        parts.append(f"{len(due_soon)} due ≤7d")
    listing = "; ".join(parts)
    severity: Literal["hard", "soft"] = "hard" if worst_days > 30 else "soft"
    mute_hint = "Run `uv run scripts/recurring.py done <slug>` when complete."
    return (
        Cue(
            key="recurring",
            severity=severity,
            command_path="scripts/recurring.py",
            message=(
                f"Recurring obligations: {listing}. "
                f"`uv run scripts/recurring.py list` to see. {mute_hint}"
            ),
        ),
        f"overdue={len(overdue)} due_soon={len(due_soon)} worst={worst_days}d; {severity} cue",
    )


def check_aggregate_freshness(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Warn before quoting aggregates that lag their subject source."""
    # Import lazily so cues.py doesn't take an import-time dep on the script.
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from aggregate_freshness import discover  # type: ignore[import-not-found]
    except ImportError as exc:
        return None, f"aggregate_freshness import failed: {exc!r}"

    payload = discover(stale_only=True)
    stale = payload.get("stale_count", 0)
    if stale == 0:
        return None, f"discovered={payload.get('discovered', 0)} stale=0; fresh"

    names = []
    for g in payload["groups"]:
        for a in g["aggregates"]:
            p = a["path"].rsplit("/", 1)[-1]
            names.append(f"{p} (-{a['days_behind']}d)")
    listing = ", ".join(names[:3])
    if len(names) > 3:
        listing += f", +{len(names) - 3} more"
    return (
        Cue(
            key="aggregate_freshness",
            severity="soft",
            command_path="protocols/local-first-architecture.md",
            message=(
                f"{stale} aggregates stale: {listing}. "
                f"Cross-check subject SOT before quoting."
            ),
        ),
        f"stale={stale}; soft cue",
    )


def _routine_rows(ov: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Load private routine rows once; return (rows, skip reason)."""
    config_path = _routine_registry(ov)
    if not config_path.is_file():
        return [], "private routine registry missing; skip"
    try:
        config = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [], f"private routine registry parse failed: {exc!r}"
    rows = config.get("routine", [])
    if not isinstance(rows, list) or not rows:
        return [], "no routines declared in private registry"
    return rows, None


def _routine_files(ov: Path, row: dict[str, Any]) -> list[Path] | None:
    """Filename-sorted artifacts for one row; None when its directory is absent."""
    directory = ov / str(row["output_dir"])
    if not directory.is_dir():
        return None
    return sorted(directory.glob(str(row.get("file_pattern", "*"))), key=lambda p: p.name)


def check_routine_outputs(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface outputs newer than the registry directory's acknowledged filename."""
    import json

    routines, skip = _routine_rows(ov)
    if skip:
        return None, skip

    ack_path = _meta_dir(ov) / "routine_acks.json"
    acks: dict[str, str] = {}
    if ack_path.is_file():
        try:
            acks = json.loads(ack_path.read_text())
        except (json.JSONDecodeError, OSError):
            acks = {}

    new_findings: list[tuple[str, str]] = []
    debug_parts: list[str] = []
    for r in routines:
        output_dir = r.get("output_dir")
        label = r.get("label", r.get("name", "?"))
        if not output_dir:
            debug_parts.append(f"{label}: missing output_dir")
            continue
        files = _routine_files(ov, r)
        if files is None:
            debug_parts.append(f"{label}: dir missing")
            continue
        if not files:
            debug_parts.append(f"{label}: no files yet")
            continue
        latest = files[-1]
        last_ack = acks.get(output_dir, "")
        if latest.name > last_ack:
            new_findings.append((latest.name, f"{label} ({latest.name})"))
            debug_parts.append(f"{label}: new={latest.name} > ack={last_ack or '∅'}")
        else:
            debug_parts.append(f"{label}: acked")

    debug = "; ".join(debug_parts)
    if not new_findings:
        return None, debug

    # Show the oldest review debt first so early registry rows cannot pin the
    # three visible slots and hide later routines indefinitely.
    new_findings.sort(key=lambda finding: finding[0])
    listing = "; ".join(finding[1] for finding in new_findings[:3])
    if len(new_findings) > 3:
        listing += f", +{len(new_findings) - 3} more"

    return (
        Cue(
            key="routine_outputs",
            severity="soft",
            command_path="_meta/routine_acks.json",
            message=(
                f"Remote cron routines 有新 output 待 review: {listing}. "
                f"读完后 update `_meta/routine_acks.json` "
                f"({{<output_dir>: <latest filename>}}) 来 mute."
            ),
        ),
        f"new={len(new_findings)}; {debug}",
    )


def check_routine_policy(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface routines lacking both enforced vault writes and acknowledged migration debt."""

    routines, skip = _routine_rows(ov)
    if skip:
        return None, skip
    violators: list[str] = []
    for r in routines:
        # Drive-write flags govern remote routines; local routines use the filesystem.
        if r.get("execution") == "local":
            continue
        if r.get("drive_write_enforced") is True:
            continue
        if r.get("needs_drive_write_update") is True:
            continue
        violators.append(str(r.get("name", "?")))
    if not violators:
        return None, f"all {len(routines)} routines compliant"
    listing = ", ".join(violators[:3])
    if len(violators) > 3:
        listing += f", +{len(violators) - 3} more"
    return (
        Cue(
            key="routine_policy",
            severity="soft",
            command_path="protocols/remote-routines.md",
            message=(
                f"{len(violators)} routine(s) without policy ack "
                f"(neither `drive_write_enforced` nor `needs_drive_write_update` set): "
                f"{listing}. Per `protocols/remote-routines.md` § Policy: every "
                f"routine MUST persist to $OV. Set the appropriate flag in "
                "the private routine registry."
            ),
        ),
        f"violators={len(violators)}/{len(routines)}; soft cue",
    )


def _latest_local_receipt(ov: Path, routine: str, declaration: dict | None = None) -> tuple[date, dict, Path] | None:
    """Load the latest compact domain receipt for one local routine."""
    routine_dir = _meta_dir(ov) / "routine_receipts" / routine
    if not routine_dir.is_dir():
        return None
    candidates: list[tuple[date, Path]] = []
    is_autoevo = routine == "autoevo-nightly"
    for receipt_path in routine_dir.glob("*.json" if is_autoevo else "*.toml"):
        try:
            receipt_date = date.fromisoformat(receipt_path.stem)
        except ValueError:
            continue
        candidates.append((receipt_date, receipt_path))
    for receipt_date, receipt_path in sorted(candidates, reverse=True):
        try:
            if is_autoevo:
                receipt = autoevo_verify.read_record(autoevo_verify.record_path(ov, receipt_date.isoformat()))
                if receipt.get("cycle_id") != receipt_path.stem:
                    return None
                verified = receipt.get("status") == "complete" and not receipt.get("errors")
                if verified:
                    autoevo_verify.verify_cycle(vault=ov, cycle=receipt_date.isoformat())
                # A projection for generic routine consumers, never a second receipt.
                receipt = {**receipt, "routine": routine, "contract_version": 3,
                           "verification": "passed" if verified else receipt.get("status"),
                           "result_summary": f"{len(receipt.get('pending', []))} findings queued; structured result {receipt.get('status')}"}
            else:
                if declaration is None:
                    raise ValueError("receipt has no output declaration")
                receipt = routine_receipts.read(receipt_path, routine=routine, cycle=receipt_path.stem,
                                               vault=ov, output_dir=declaration.get("output_dir"),
                                               file_pattern=declaration.get("file_pattern", "*.md"))
        except routine_receipts.IdentityMismatch:
            continue
        except (OSError, ValueError, autoevo_verify.VerificationError):
            if is_autoevo:
                return None
            return receipt_date, {"verification": "needs_review"}, receipt_path
        return receipt_date, receipt, receipt_path
    return None


def _cron_expressions(value: object) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return value
    return []


def _scheduled_dates(cron: object, start: date, now: datetime, timezone_name: str | None = None) -> list[date]:
    dates: set[date] = set()
    for expression in _cron_expressions(cron):
        dates.update(cron_spec.scheduled_dates(expression, start, now, timezone_name))
    return sorted(dates)


def check_routine_staleness(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Detect stale output artifacts from declared cadence; this does not prove a run fired."""

    routines, skip = _routine_rows(ov)
    if skip:
        return None, skip

    stale: list[str] = []
    debug_parts: list[str] = []

    for r in routines:
        name = r.get("name", "?")
        if r.get("adapter") == "autoevo":
            continue  # Conditional notes; Prefect and receipt checks own execution health.
        label = r.get("label", r.get("name", "?"))
        output_dir = r.get("output_dir")
        cron = r.get("cron", "")
        is_local = r.get("execution") == "local"
        if not output_dir or not cron:
            debug_parts.append(f"{label}: missing output_dir or cron")
            continue

        cadence_days = _estimate_cadence_days(cron)
        if cadence_days is None:
            debug_parts.append(f"{label}: unparseable cron")
            continue

        tolerance = max(2, cadence_days)
        threshold = cadence_days + tolerance
        latest_receipt = _latest_local_receipt(ov, str(name), r) if is_local else None

        files = _routine_files(ov, r)
        if files is None:
            stale.append(f"{label} (output dir missing)")
            debug_parts.append(f"{label}: dir missing; cadence={cadence_days}d")
            continue

        if not files:
            stale.append(f"{label} (no output files)")
            debug_parts.append(f"{label}: no files; cadence={cadence_days}d")
            continue

        latest_name = files[-1].name
        latest_date = date_in_text(latest_name)
        if latest_date is None:
            debug_parts.append(f"{label}: can't parse date from {latest_name}")
            continue

        if latest_receipt is not None:
            receipt_date, receipt, _receipt_path = latest_receipt
            if receipt.get("verification") == "passed" and receipt_date > latest_date:
                stale.append(
                    f"{label} (verified receipt {receipt_date} newer than output {latest_date})"
                )
                debug_parts.append(
                    f"{label}: verified receipt={receipt_date} > output={latest_date}"
                )
                continue

        age = (today - latest_date).days
        if age > threshold:
            stale.append(
                f"{label} (last output {age}d ago, expected every {cadence_days}d)"
            )
            debug_parts.append(f"{label}: age={age}d > threshold={threshold}d")
        else:
            debug_parts.append(f"{label}: age={age}d <= threshold={threshold}d; ok")

    debug = "; ".join(debug_parts)
    if not stale:
        return None, debug

    listing = "; ".join(stale[:3])
    if len(stale) > 3:
        listing += f", +{len(stale) - 3} more"

    return (
        Cue(
            key="routine_staleness",
            severity="hard",
            command_path="_tools/routines/registry.toml",
            message=(
                f"{len(stale)} routine(s) with missing/stale output: {listing}. "
                f"Check the active scheduler in the private routine registry, then inspect its "
                f"local Prefect state or cloud session and connector logs."
            ),
        ),
        f"stale={len(stale)}; {debug}",
    )


def check_routine_hitrate(
    ov: Path,
    today: date,
    *,
    now: datetime | None = None,
) -> tuple[Cue | None, str]:
    """Measure output-date coverage for routines frequent enough to have usable samples."""

    routines, skip = _routine_rows(ov)
    if skip:
        return None, skip

    now = now or datetime.now().astimezone()
    degraded: list[str] = []
    debug_parts: list[str] = []

    for r in routines:
        if r.get("adapter") == "autoevo":
            continue  # Empty verified cycles intentionally have no Markdown output.
        label = r.get("label", r.get("name", "?"))
        output_dir = r.get("output_dir")
        cron = r.get("cron", "")
        if not output_dir or not cron:
            continue

        cadence_days = _estimate_cadence_days(cron)
        if cadence_days is None or cadence_days > 7:
            debug_parts.append(f"{label}: cadence={cadence_days}d; skip hitrate")
            continue

        files = _routine_files(ov, r)
        if files is None:
            continue  # staleness cue handles this

        max_lookback = max(14, 3 * cadence_days)
        zone_name = r.get("timezone")
        zone = cron_spec.schedule_zone(_cron_expressions(cron)[0], now.astimezone().tzinfo, zone_name)
        cycle_today = now.astimezone(zone).date() if zone_name is not None else today
        cutoff = cycle_today - timedelta(days=max_lookback - 1)

        dated_files: list[date] = []
        for f in files:
            fd = date_in_text(f.name)
            if fd is not None:
                dated_files.append(fd)

        if not dated_files:
            continue  # staleness cue handles this

        # Cap lookback to oldest file date so new routines aren't penalized
        # for not existing before their first output.
        oldest_file = min(dated_files)
        effective_start = max(cutoff, oldest_file)
        effective_lookback = (cycle_today - effective_start).days + 1

        scheduled = _scheduled_dates(cron, effective_start, now, zone_name)
        expected_dates = set(scheduled)
        expected = len(expected_dates)
        if expected < 3:
            debug_parts.append(
                f"{label}: expected={expected} in {effective_lookback}d; too few samples"
            )
            continue

        actual = len(set(dated_files) & expected_dates)
        rate = actual / expected if expected > 0 else 1.0

        if rate < 0.70:
            pct = int(rate * 100)
            degraded.append(f"{label} ({actual}/{expected} expected dates with output, {pct}%)")
            debug_parts.append(
                f"{label}: {actual}/{expected} in {effective_lookback}d = {pct}%; degraded"
            )
        else:
            pct = int(rate * 100)
            debug_parts.append(
                f"{label}: {actual}/{expected} in {effective_lookback}d = {pct}%; ok"
            )

    debug = "; ".join(debug_parts)
    if not degraded:
        return None, debug

    listing = "; ".join(degraded[:3])
    if len(degraded) > 3:
        listing += f", +{len(degraded) - 3} more"

    return (
        Cue(
            key="routine_hitrate",
            severity="soft",
            command_path="_tools/routines/registry.toml",
            message=(
                f"{len(degraded)} routine(s) with degraded output-date coverage: {listing}. "
                f"Artifact dates alone do not establish scheduler execution. "
                f"Inspect local Prefect state or cloud session and connector logs."
            ),
        ),
        f"degraded={len(degraded)}; {debug}",
    )


def _estimate_cadence_days(cron: object) -> int | None:
    expressions = _cron_expressions(cron)
    if not expressions:
        return None
    cadences = [cron_spec.estimate_cadence_days(expression) for expression in expressions]
    if any(cadence is None for cadence in cadences):
        return None
    return min(cadence for cadence in cadences if cadence is not None)


def check_autoevo_pending(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface pending Autoevo decisions, including malformed dates and repeated skips."""

    config_path = _meta_dir(ov) / "autoevo_pending.toml"
    if not config_path.is_file():
        return None, "_meta/autoevo_pending.toml missing; skip"

    try:
        config = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        # Corruption blocks review and append; silence would hide both failures.
        # Route a hard cue to manual repair.
        return (
            Cue(
                key="autoevo_pending",
                severity="hard",
                command_path="_meta/autoevo_pending.toml",
                message=(
                    f"Autoevo pending queue file is corrupted "
                    f"(`_meta/autoevo_pending.toml`): {type(exc).__name__}. "
                    f"`/autoevo-review` and the Autoevo routine cannot proceed. "
                    f"Repair by hand (TOML syntax), or back up + restart with an empty file."
                ),
            ),
            f"autoevo_pending.toml parse failed: {exc!r}; hard cue",
        )

    entries = config.get("pending", [])
    if not entries:
        return None, "no pending entries declared"

    # Filter to actually-pending entries.
    pending = [e for e in entries if e.get("status", "pending") == "pending"]
    if not pending:
        return None, f"all {len(entries)} entries resolved"

    # Group by category, track oldest age, count entries with unparseable dates,
    # count entries the user has repeatedly skipped (auto-dismiss threshold).
    counts: dict[str, int] = {}
    oldest_age = 0
    corrupt_dates = 0
    repeat_skips = 0
    defaults = 0
    earliest_default: date | None = None
    for e in pending:
        cat = str(e.get("category", "unknown"))
        counts[cat] = counts.get(cat, 0) + 1
        if e.get("default_action"):
            try:
                deadline = date.fromisoformat(str(e.get("default_at")))
            except (ValueError, TypeError):
                deadline = None
            if deadline is not None:
                defaults += 1
                if earliest_default is None or deadline < earliest_default:
                    earliest_default = deadline
        proposed = e.get("proposed_at", "")
        try:
            proposed_date = date.fromisoformat(str(proposed))
            age = (today - proposed_date).days
            if age > oldest_age:
                oldest_age = age
        except (ValueError, TypeError):
            corrupt_dates += 1
        # Escalate repeated skips before the next review can auto-dismiss them.
        try:
            if int(e.get("surface_count", 0)) >= 3:
                repeat_skips += 1
        except (ValueError, TypeError):
            continue

    listing = ", ".join(
        f"{cat}: {n}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    # Escalate to `hard` on age (>14d), corrupt dates (parsability lost), OR
    # repeat-skip entries (3+ skips reach auto-dismiss next /autoevo-review).
    severity: Literal["hard", "soft"] = (
        "hard" if (oldest_age > 14 or corrupt_dates > 0 or repeat_skips > 0) else "soft"
    )
    age_note = f"oldest {oldest_age}d" if oldest_age > 0 else "fresh"
    corrupt_note = f"; {corrupt_dates} corrupt dates" if corrupt_dates > 0 else ""
    skip_note = f"; {repeat_skips} ≥3 skips" if repeat_skips > 0 else ""
    default_note = ""
    if defaults and earliest_default is not None:
        default_note = (
            f" 其中 {defaults} 条带默认动作 (stale 标记), 最早 {earliest_default.isoformat()} 由 nightly 自动执行; "
            f"`/autoevo-review` 里 skip 即否决, defer 顺延 14 天."
        )
    return (
        Cue(
            key="autoevo_pending",
            severity=severity,
            command_path="skills/autoevo-review/SKILL.md",
            message=(
                f"{len(pending)} pending autoevo decisions ({listing}; {age_note}{corrupt_note}{skip_note}). "
                f"`/autoevo-review` to triage.{default_note}"
            ),
        ),
        f"pending={len(pending)} oldest_age={oldest_age} corrupt={corrupt_dates} skips={repeat_skips} defaults={defaults}; {severity} cue",
    )


def check_autoevo_ran(
    ov: Path,
    today: date,
    *,
    now: datetime | None = None,
) -> tuple[Cue | None, str]:
    """After 06:00, reconcile today's latest Prefect attempt with its JSON result."""
    now = now or datetime.now()
    if now.hour < 6:
        return None, "before 06:00 local; skip"
    zone = now.tzinfo if now.tzinfo is not None else now.astimezone().tzinfo
    try:
        record = autoevo_verify.record_path(ov, today.isoformat())
    except (autoevo_verify.VerificationError, OSError) as exc:
        return Cue("autoevo_ran", "hard", "protocols/autoevo.md", "Autoevo's result path is unsafe or unreadable; inspect it before any rerun."), str(exc)
    record_display = str(record.resolve().relative_to(ov.resolve()))
    installed = record.parent.is_dir() and any(record.parent.iterdir())
    since = datetime.combine(today - timedelta(days=29), datetime.min.time(), tzinfo=zone)
    try:
        runs = [
            run for run in routine_status.recent_runs(since, model_only=True, limit=200)
            if run.get("routine") == "autoevo-nightly"
        ]
    except routine_status.StatusUnavailable as exc:
        if not installed:
            return None, "Prefect unavailable with no Autoevo installation evidence; skip"
        return (
            Cue("autoevo_ran", "soft", "scripts/launchd/README.md", "Autoevo Prefect status is unavailable; inspect the local service and logs."),
            f"Prefect status unavailable: {exc}",
        )
    if not runs and not installed:
        return None, "no Prefect runs or structured results; bot not installed yet"

    def run_day(run: dict[str, Any]) -> date | None:
        value = run.get("expected_start_time") or run.get("start_time")
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(zone).date()

    daily: dict[date, dict[str, Any]] = {}
    for run in runs:  # recent_runs is newest first; the first run wins each day.
        if (day := run_day(run)) is not None:
            daily.setdefault(day, run)
    latest = daily.get(today)
    if latest is None:
        return (
            Cue("autoevo_ran", "soft", "scripts/launchd/README.md", f"Nightly Autoevo has no Prefect attempt for {today.isoformat()}; inspect the local schedule and runner."),
            "no Prefect attempt today",
        )

    def blocker(run: dict[str, Any]) -> str | None:
        state_name = str(run.get("state_name") or "").lower().replace("_", "").replace(" ", "")
        if state_name not in {"deferred", "notready"}:
            return None
        message = str(run.get("message") or "").strip()
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("gate"), str):
            return payload["gate"]
        match = re.search(r"(?:deferred by|publication deferred:|notready:)\s*`?([a-z][a-z0-9_]*)", message, re.I)
        return match.group(1) if match else "not_ready"

    if gate := blocker(latest):
        streak = 1
        for back in range(1, 30):
            prior = daily.get(today - timedelta(days=back))
            if prior is None or blocker(prior) != gate:
                break
            streak += 1
        fixes = {
            "session_active": "wait for the active session to finish",
            "session_lock_unsafe": "restore a real vault `_meta` directory and remove any symlinked session lock",
            "git_not_worktree": "repair the vault Git worktree",
            "privacy_hits": "resolve the privacy check finding",
            "semantic_unavailable": "restore the local semantic index",
            "environment_unavailable": "check local storage and File Provider health",
        }
        hard = streak >= 3
        fix = fixes.get(gate, "inspect the Prefect NotReady event and deterministic preflight")
        return (
            Cue(
                "autoevo_ran",
                "hard" if hard else "soft",
                "scripts/launchd/README.md",
                f"Nightly Autoevo is NotReady on `{gate}` ({streak} consecutive day{'s' if streak != 1 else ''}). Fix: {fix}.",
            ),
            f"blocker {gate} streak={streak}; {'hard' if hard else 'soft'} cue",
        )

    state = str(latest.get("state") or "UNKNOWN").upper()
    state_name = str(latest.get("state_name") or state)
    detail = str(latest.get("message") or "")[:160]
    if state != "COMPLETED":
        suffix = f": {detail}" if detail else ""
        return (
            Cue("autoevo_ran", "soft", "scripts/launchd/README.md", f"Today's latest Autoevo attempt is `{state_name}`{suffix}. Inspect Prefect before retrying."),
            f"latest Prefect state={state_name}",
        )
    if not record.is_file():
        return (
            Cue("autoevo_ran", "soft", record_display, "Prefect completed today's Autoevo run, but its structured JSON result is missing; inspect the run before retrying."),
            f"completed Prefect run missing {record.name}",
        )
    try:
        proof = autoevo_verify.verify_cycle(vault=ov, cycle=today.isoformat())
    except (autoevo_verify.VerificationError, OSError, KeyError, TypeError, ValueError) as exc:
        return (
            Cue("autoevo_ran", "soft", record_display, f"Today's Autoevo result is not verified: {str(exc)[:160]}. Inspect the JSON result and Prefect run before retrying."),
            f"structured result verification failed: {exc}",
        )
    return None, f"latest Prefect attempt completed; structured result verified ({len(proof.get('operations', {}))} operations)"


def _recap_local_runs(ov: Path, today: date, verbose: bool = False) -> list[str]:
    """Summarize recent successful domain results, never parse a human report."""
    runs_dir = _meta_dir(ov) / "routine_receipts"
    if not runs_dir.is_dir():
        return []
    rows, _ = _routine_rows(ov)
    declarations = {row.get("name"): row for row in rows if isinstance(row, dict)}
    recaps = []
    for routine in sorted(runs_dir.iterdir()):
        if not routine.is_dir():
            continue
        latest = _latest_local_receipt(ov, routine.name, declarations.get(routine.name))
        if latest is None:
            continue
        cycle, receipt, _ = latest
        if cycle not in {today, today - timedelta(days=1)} or receipt.get("verification") != "passed":
            continue
        duration = receipt.get("duration_seconds")
        summary = receipt.get("result_summary", "")
        when = "today" if cycle == today else "yesterday"
        recap = f"{routine.name} ran {when} via Prefect"
        if routine.name != "autoevo-nightly" and receipt.get("contract_version") == 3:
            recap += " (v3: content unbound)"
        recap += f" ({duration}s)" if duration else ""
        recap += f": {summary}" if summary else ""
        recaps.append(recap)
    if verbose:
        for recap in recaps:
            print(f"# debug: recap: {recap}", file=sys.stderr)
    return recaps


def check_local_routine_missed(
    ov: Path,
    today: date,
    *,
    now: datetime | None = None,
) -> tuple[Cue | None, str]:
    """Detect missing verified artifact receipts; Prefect remains authoritative for run state."""
    now = now or datetime.now().astimezone()
    if now.hour < 6:
        return None, "before 06:00 local; skip"
    config_path = _routine_registry(ov)
    try:
        config = tomllib.loads(config_path.read_text())
    except FileNotFoundError:
        return None, "private routine registry missing; skip"
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return None, f"private routine registry parse failed: {exc!r}"
    routines = [r for r in config.get("routine", []) if r.get("execution") == "local" and r.get("runner") == "model"]
    receipts = _meta_dir(ov) / "routine_receipts"
    if not routines or not receipts.is_dir():
        return None, "no local model receipts; skip"
    missed: list[str] = []
    debug_parts: list[str] = []
    for row in routines:
        name = str(row.get("name", "?"))
        label = str(row.get("label", name))
        cron = row.get("cron", "")
        cadence = _estimate_cadence_days(cron)
        if cadence is None:
            debug_parts.append(f"{label}: health cron unavailable")
            continue
        start = today - timedelta(days=max(366, 3 * cadence))
        due = _scheduled_dates(cron, start, now, row.get("timezone"))
        if not due:
            continue
        expected = due[-1]
        latest = _latest_local_receipt(ov, name, row)
        if latest is None or latest[0] < expected:
            missed.append(f"{label} (no verified receipt for {expected})")
            debug_parts.append(f"{label}: expected={expected}; receipt absent or old")
            continue
        cycle, receipt, _ = latest
        verification = receipt.get("verification")
        if verification == "passed":
            debug_parts.append(f"{label}: {cycle} verified")
        elif verification == "blocked":
            blocker = str(receipt.get("blocker") or "domain preflight")
            missed.append(f"{label} (deferred on {cycle}: {blocker})")
            debug_parts.append(f"{label}: {cycle} blocked")
        else:
            missed.append(f"{label} (receipt verification {verification or 'unknown'} on {cycle})")
            debug_parts.append(f"{label}: {cycle} verification={verification}")
    if not missed:
        return None, "; ".join(debug_parts)
    listing = "; ".join(missed[:3]) + (f", +{len(missed) - 3} more" if len(missed) > 3 else "")
    return (
        Cue(
            key="local_routine_missed",
            severity="soft",
            command_path="scripts/launchd/README.md",
            message=(
                f"{len(missed)} local routine(s) lack a verified artifact: {listing}. "
                "Inspect the authoritative Prefect flow state and screened logs before rerunning; "
                "a model attempt is never retried automatically."
            ),
        ),
        f"missed={len(missed)}; {'; '.join(debug_parts)}",
    )


def check_career_growth(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Prompt growth review from the private plan, respecting completed reviews and snoozes."""
    career_dir = tier("career")
    plan_candidates = [
        path
        for path in career_dir.glob("*.md")
        if path.is_file() and "plan" in path.stem.casefold()
    ]
    if not plan_candidates:
        return None, "no career plan found; goal not set up"
    plan_path = max(plan_candidates, key=lambda path: path.stat().st_mtime)
    try:
        plan_ref = plan_path.relative_to(ov).as_posix()
    except ValueError:
        plan_ref = plan_path.as_posix()

    if not tier("reflections").is_dir():
        return None, "reflections dir missing; skip"

    reviews = tier_files("reflections", "*-growth-review.md")
    days_since: int | None = None
    if reviews:
        try:
            latest_date = datetime.strptime(reviews[-1].name[:10], "%Y-%m-%d").date()
            days_since = (today - latest_date).days
        except ValueError:
            days_since = None

    is_sunday = today.weekday() == 6  # Mon=0 .. Sun=6

    if days_since is None:
        fire = is_sunday
        reason = f"no prior growth-review; sunday={is_sunday}"
    elif is_sunday and days_since >= 6:
        fire = True
        reason = f"sunday, days_since={days_since}"
    elif days_since > 9:
        fire = True
        reason = f"missed sunday, days_since={days_since}"
    else:
        fire = False
        reason = f"days_since={days_since}, weekday={today.weekday()}; fresh"

    if not fire:
        return None, reason

    return (
        Cue(
            key="career_growth",
            severity="soft",
            command_path=plan_ref,
            message=(
                "周日 growth review: 对照当前 career plan,回顾过去一周的学习、"
                "工程产出、前瞻设计和公开贡献,看看有没有忘记、偏离或需要调整"
                "路线。现在过一下吗?"
            ),
        ),
        reason,
    )


    # Session-start cue registry.


def check_intent_misses(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface recurring unrouted requests through the shared intent-misses aggregation."""
    log_dir = _meta_dir(ov) / "intent_routes"
    if not log_dir.is_dir():
        return None, "no route log; skip"
    events = intent_coverage.load_route_events(since=today - timedelta(days=14), dirs=[log_dir])
    stats = intent_coverage.aggregate_route_events(events)
    repeaters = stats["repeaters"]
    misses = len(events) - stats["routed_count"]
    threshold = intent_coverage.INTENT_MISS_DISTINCT_DAYS_THRESHOLD
    if not repeaters:
        return None, f"{misses} unrouted in 14d, none on {threshold}+ days; silent"
    return (
        Cue(
            key="intent_misses",
            severity="soft",
            command_path="protocols/intent-coverage.md",
            message=(
                f"过去 14 天有 {len(repeaters)} 个 `/hi` 请求在 {threshold}+ 天反复出现却没命中 catalog 行 "
                f"(共 {misses} 次 general / clarified / corrected). "
                f"跑 `uv run scripts/intent_coverage.py intent-misses --propose` "
                f"看该补 description、加 example 还是写新 procedure."
            ),
        ),
        f"{len(repeaters)} repeater(s), {misses} unrouted in 14d",
    )


def check_vault_layout(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Split-layout health: a Git vault outside sync folders, raw_store links intact."""
    from zk_audit import check_layout  # type: ignore[import-not-found]

    findings = check_layout(ov, raw_store())
    if not findings:
        return None, "layout ok"
    first = findings[0]
    return (
        Cue(
            key="vault_layout",
            severity="hard",
            command_path="scripts/zk_audit.py",
            message=(
                f"Vault 布局有 {len(findings)} 个问题 (例: {first.where}: {first.detail}). "
                "跑 `uv run scripts/zk_audit.py`; 缺失的链接可加 `--fix-links`."
            ),
        ),
        f"{len(findings)} layout finding(s)",
    )


def check_wiki_attention(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Wiki claims the nightly review cannot settle: flagged reviews and expired evidence."""
    import trust  # type: ignore[import-not-found]
    import wiki_review  # type: ignore[import-not-found]

    items = []
    for path in wiki_review.notes(ov / tier_segments().get("wiki", "wiki")):
        note = trust.parse_wiki_note(path, today)
        items += [f"[[{note.title}#^c{number}]] {reason}" for number, reason in wiki_review.attention(note, today)]
    if not items:
        return None, "no wiki claim needs attention"
    return (
        Cue(
            key="wiki_attention",
            severity="soft",
            command_path="skills/lint/SKILL.md",
            message=(
                f"Wiki 有 {len(items)} 个 claim 需要你决定 (例: {items[0]}). "
                "flagged 的理由在夜间 `autoevo-applied-*` 回执里; 跑 `/lint` 看全部."
            ),
            count=len(items),
            items=items,
        ),
        f"{len(items)} wiki claim(s) need attention",
    )


def check_promote_candidates(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Working notes your own thinking keeps linking to that no wiki entry cites yet."""
    from _reflect import read_head  # type: ignore[import-not-found]
    from staleness import promotion_candidates  # type: ignore[import-not-found]

    ranked = sorted(promotion_candidates(today).items(), key=lambda item: (-item[1], item[0].as_posix()))
    if not ranked:
        return None, "no promotion candidates"
    items = [f"[[{read_head(path)[0] or path.stem}]] ({count})" for path, count in ranked[:5]]
    return (
        Cue(
            key="promote_candidates",
            severity="soft",
            command_path="skills/promote/SKILL.md",
            message=(
                f"{len(ranked)} 条笔记你反复链接、但还没有 wiki 引用 (例: {items[0]}). "
                "想沉淀成 wiki 的话跑 `/promote`."
            ),
            count=len(ranked),
            items=items,
        ),
        f"{len(ranked)} promotion candidate(s)",
    )


def check_routine_failures(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface the latest failed Prefect run per local model or process routine."""
    zone = datetime.now().astimezone().tzinfo
    since = datetime.combine(today - timedelta(days=7), datetime.min.time(), tzinfo=zone)
    try:
        runs = routine_status.recent_runs(since, model_only=False)
    except routine_status.StatusUnavailable as exc:
        if not (_meta_dir(ov) / "routine_receipts").is_dir() and not os.environ.get("PREFECT_API_URL"):
            return None, "Prefect API unavailable; no installation evidence"
        return (
            Cue(
                key="routine_failures",
                severity="soft",
                command_path="scripts/launchd/README.md",
                message="Local Prefect status is unavailable; verify the API server and deployment runner before trusting routine freshness.",
            ),
            f"Prefect API unavailable: {exc}",
        )
    latest: dict[str, dict] = {}
    for run in runs:
        name = str(run.get("routine") or "")
        if name and name not in latest:
            latest[name] = run
    failed = [run for run in latest.values() if run.get("state") in {"FAILED", "CRASHED", "CANCELLED"}
              and run.get("state_name") != "Deferred"]
    if not failed:
        return None, f"queried={len(runs)} latest={len(latest)} failed=0"
    listing = "; ".join(
        f"{run['routine']} ({run['state_name']}: {str(run.get('message') or '')[:100]})" for run in failed[:3]
    )
    if len(failed) > 3:
        listing += f", +{len(failed) - 3} more"
    return (
        Cue(
            key="routine_failures",
            severity="soft",
            command_path="scripts/launchd/README.md",
            message=f"{len(failed)} local routine(s) have a latest failed Prefect run: {listing}. Review effects before manually rerunning a model attempt.",
        ),
        f"queried={len(runs)} latest={len(latest)} failed={len(failed)}",
    )


CHECKS = [
    ("vault_layout", check_vault_layout),
    ("weekly", check_weekly),
    ("intent_misses", check_intent_misses),
    ("reflect_intake", check_reflect_intake),
    ("recurring", check_recurring),
    ("aggregate_freshness", check_aggregate_freshness),
    ("routine_outputs", check_routine_outputs),
    ("routine_staleness", check_routine_staleness),
    ("routine_hitrate", check_routine_hitrate),
    ("routine_policy", check_routine_policy),
    ("autoevo_pending", check_autoevo_pending),
    ("wiki_attention", check_wiki_attention),
    ("promote_candidates", check_promote_candidates),
    ("autoevo_ran", check_autoevo_ran),
    ("local_routine_missed", check_local_routine_missed),
    ("routine_failures", check_routine_failures),
    ("career_growth", check_career_growth),
]


# --- snooze: per-key, per-day suppression ---------------------------------


def _snooze_path(ov: Path) -> Path:
    return _meta_dir(ov) / "cue_snooze.json"


def _load_snoozes(ov: Path) -> dict[str, str]:
    p = _snooze_path(ov)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return {
            k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)
        }
    except (json.JSONDecodeError, OSError):
        return {}


def _is_snoozed(snoozes: dict[str, str], key: str, today: date) -> bool:
    val = snoozes.get(key)
    if not val:
        return False
    try:
        return date.fromisoformat(val) >= today
    except ValueError:
        return False


def snooze_cue(ov: Path, key: str, until: date) -> None:
    p = _snooze_path(ov)
    p.parent.mkdir(parents=True, exist_ok=True)
    snoozes = _load_snoozes(ov)
    snoozes[key] = until.isoformat()
    p.write_text(
        json.dumps(snoozes, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


# --- main -----------------------------------------------------------------


def cue_errors_log_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "atelier" / "cue_errors.jsonl"


def record_cue_errors(errors: list[tuple[str, str]], today: date, log_path: Path | None = None) -> Path | None:
    """Append one JSON line per crashed check; machine-local, never in the vault."""
    path = log_path or cue_errors_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for name, detail in errors:
                handle.write(json.dumps({"date": today.isoformat(), "check": name, "error": detail[:400]}) + "\n")
        return path
    except OSError:
        return None


def cue_errors_cue(errors: list[tuple[str, str]], log_path: Path | None) -> Cue:
    names = ", ".join(name for name, _ in errors)
    where = f" 详情在 `{log_path}`." if log_path else " (日志写入也失败了)."
    return Cue(
        key="cue_errors",
        severity="hard",
        command_path="scripts/cues.py",
        message=(
            f"{len(errors)} 个 cue 检查自身报错 ({names}), 它们的提示可能缺失.{where} "
            f"跑 `uv run scripts/cues.py --verbose --only <name>` 复现."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quiet-by-default cue checks for Claude /hi and Codex $hi session start."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON array instead of tab-separated lines.",
    )
    parser.add_argument(
        "--hook",
        action="store_true",
        help="Emit Claude Code or Codex SessionStart hook output: when cues fire, "
        "print a `hookSpecificOutput.additionalContext` JSON; when silent, "
        "print nothing. Exit 0 always.",
    )
    parser.add_argument(
        "--runtime",
        choices=("auto", "claude", "codex"),
        default="auto",
        help="Render registered workflow references for this runtime (default: auto).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-check reasoning to stderr.",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Run only the named cue (debug aid).",
    )
    # Snoozes suppress matching fired cues until their stored expiry.
    if argv is None:
        argv_list = sys.argv[1:]
    else:
        argv_list = list(argv)
    if argv_list and argv_list[0] == "snooze":
        if not os.environ.get("OV"):
            print("ERROR: $OV not set; cannot snooze.", file=sys.stderr)
            return 2
        if len(argv_list) < 2:
            print("ERROR: snooze requires <key> argument", file=sys.stderr)
            return 2
        key = argv_list[1]
        if key not in {name for name, _ in CHECKS}:
            print(
                f"ERROR: unknown cue `{key}`; valid: {sorted({n for n, _ in CHECKS})}",
                file=sys.stderr,
            )
            return 2
        days = 1
        if "--days" in argv_list:
            try:
                days = int(argv_list[argv_list.index("--days") + 1])
            except (ValueError, IndexError):
                print("ERROR: --days requires an integer", file=sys.stderr)
                return 2
        ov = vault_root()
        until = date.today().fromordinal(date.today().toordinal() + days)
        snooze_cue(ov, key, until)
        print(f"snoozed `{key}` until {until.isoformat()}")
        return 0

    args = parser.parse_args(argv)
    output_runtime = _resolve_output_runtime(args.runtime)

    if not os.environ.get("OV"):
        return 0
    ov = vault_root()
    today = date.today()

    snoozes = _load_snoozes(ov)

    # Hooks measure user idle time; headless routines must not refresh their own lock.
    if args.hook:
        _touch_session_lock(args.verbose, "SessionStart")

    fired: list[Cue] = []
    errors: list[tuple[str, str]] = []
    for name, fn in CHECKS:
        if args.only and name != args.only:
            continue
        try:
            cue, reason = fn(ov, today)
        except Exception as exc:  # never let a cue check break /hi
            errors.append((name, f"{type(exc).__name__}: {exc}"))
            if args.verbose:
                print(f"# debug: {name} raised {exc!r}", file=sys.stderr)
            continue
        if cue and _is_snoozed(snoozes, name, today):
            if args.verbose:
                print(f"# debug: {name} SNOOZED until {snoozes[name]}", file=sys.stderr)
            continue
        if args.verbose:
            tag = "FIRED" if cue else "silent"
            print(f"# debug: {name} {tag}: {reason}", file=sys.stderr)
        if cue:
            cue.message = _format_runtime_message(cue.message, output_runtime)
            fired.append(cue)
    if errors:
        # Persist and surface check failures so a broken cue cannot report false silence.
        log_path = record_cue_errors(errors, today)
        fired.append(cue_errors_cue(errors, log_path))

    if args.hook:
        # Shared SessionStart hook protocol. Injects fired cues plus recent
        # run recaps as context on the next Claude Code or Codex model call.
        recaps = _recap_local_runs(ov, today, verbose=args.verbose)
        if not fired and not recaps:
            return 0
        sections: list[str] = []
        if fired:
            lines = [f"- {c.message} (route: `{c.command_path}`)" for c in fired]
            sections.append("Session-start cues (atelier):\n" + "\n".join(lines))
        if recaps:
            recap_lines = [f"- {r}" for r in recaps]
            sections.append("Recent local routine runs:\n" + "\n".join(recap_lines))
        context = "\n".join(sections)
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
        print(json.dumps(payload, ensure_ascii=False))
    elif args.json:
        print(json.dumps([{k: v for k, v in asdict(c).items() if v is not None} for c in fired], ensure_ascii=False))
    else:
        for c in fired:
            print(f"{c.key}\t{c.severity}\t{c.command_path}\t{c.message}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
