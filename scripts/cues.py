#!/usr/bin/env python3
"""cues.py: Unified, quiet-by-default cue checker for native hi session start.

Why this exists: Claude `/hi` and Codex `$hi` need to surface "you forgot to run X"
nudges (weekly review overdue, mobile-capture inbox pending). The old
pattern was inline Bash blocks in `.claude/commands/hi.md` that printed
debug lines (`days_since=4 latest=...`, `zettelm_pending=0`) into the
main conversation context on every invocation. That pollutes the model's
context window with state that means nothing to the user 90% of the time.

This script collapses every session-start cue into one call. It emits
NOTHING to stdout when no cue should fire. When a cue fires, it prints
one tab-separated line per cue:

    <key>\\t<severity>\\t<command_path>\\t<user-facing message>

The orchestrator parses each line and routes via the standard yes/no UI.
In the no-cue case the orchestrator sees zero output and proceeds
silently to the Step 1 menu — main context cost is bounded by the
command invocation itself, not the state of the vault.

Add new cues by appending a `check_*` function and registering it in
`CHECKS`. Each function returns either `None` (silent) or a `Cue`.

Output formats:
    default            tab-separated lines (one per fired cue)
    --json             JSON array of objects (for hook consumption)
    --verbose          add a `# debug: ...` line per check explaining the decision

Snooze:
    cues.py snooze <key> [--days N]    suppress a cue until N days from today

Snooze state lives at `$OV/_meta/cue_snooze.json`. Useful for soft cues
where the user has reviewed the state and accepted the lag (e.g.,
aggregate_freshness when the underlying aggregate update is queued).

Exits 0 always. Failing to find the vault still exits 0 with no output
so an unconfigured environment never blocks either native hi workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

# Allow running as `uv run scripts/cues.py` from atelier root.
sys.path.insert(0, str(Path(__file__).parent))
from _paths import tier, tier_files, tier_segments, vault_root  # type: ignore[import-not-found]  # noqa: E402
import cron_spec  # noqa: E402
import intent_coverage  # noqa: E402
from routine_claim import validate_claim  # noqa: E402


@dataclass
class Cue:
    key: str
    severity: str  # "hard" | "soft"
    command_path: str  # relative path to the command file to route into on Yes
    message: str  # user-facing Chinese prompt


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
        from registries import load_commands

        commands = load_commands()
    except Exception:  # noqa: BLE001  (cosmetic rendering stays fail-open)
        return message
    for name, entry in sorted(commands.items(), key=lambda item: -len(item[0])):
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


def _touch_session_lock(verbose: bool, context: str) -> None:
    """Touch the session-active marker unless the autoevo runtime asked us not to.

    Shared by the SessionStart hook and the UserPromptSubmit `--touch-lock`
    refresh; the two paths desynced once when the logic lived in two copies.
    """
    if os.environ.get("ATELIER_SKIP_LOCK_TOUCH"):
        if verbose:
            print(
                f"# debug: {context} lock touch skipped (ATELIER_SKIP_LOCK_TOUCH set)",
                file=sys.stderr,
            )
        return
    try:
        cache_dir = tier("cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "atelier-session-lock").touch()
    except OSError as exc:
        if verbose:
            print(f"# debug: session-lock touch failed: {exc!r}", file=sys.stderr)


# --- individual checks ----------------------------------------------------


def check_weekly(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Weekly review cadence cue.

    Hard floor: >10 days since last weekly, or no weekly ever.
    Soft cue: >6 days since last weekly AND today is Sunday or Monday.
    """
    if not tier("reflections").is_dir():
        return None, "reflections dir missing; skip weekly cue"

    weeklies = tier_files("reflections", "*-weekly.md")
    if not weeklies:
        return (
            Cue(
                key="weekly",
                severity="hard",
                command_path=".claude/commands/weekly.md",
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
                command_path=".claude/commands/weekly.md",
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
                command_path=".claude/commands/weekly.md",
                message=(
                    f"提示: 上次 weekly 是 {days_since} 天前. "
                    f"想现在跑 `/weekly` 把这周补齐吗?"
                ),
            ),
            f"days_since={days_since}, weekday={weekday}; soft cue",
        )

    return None, f"days_since={days_since}, weekday={weekday}; fresh"



def check_zettelm(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Zettelm (mobile capture submodule) pending-digest cue.

    Hard floor: >=3 pending files, or oldest file is >7 days old.
    Soft cue: >=1 pending file.
    Silent: empty.
    """
    zm = tier("zettelm")
    if not zm.is_dir():
        return None, "zettelm/ missing; skip"

    exts = (".md", ".pdf", ".jpg", ".jpeg", ".png", ".heic", ".m4a", ".mp3")
    ignored = {"README.md", ".gitignore", ".gitattributes"}

    pending = [
        p
        for p in zm.iterdir()
        if p.is_file() and p.suffix.lower() in exts and p.name not in ignored
    ]

    if not pending:
        return None, "zettelm empty; fresh"

    n = len(pending)
    oldest_mtime = min(p.stat().st_mtime for p in pending)
    oldest_age_days = (today - date.fromtimestamp(oldest_mtime)).days

    hard = n >= 3 or oldest_age_days > 7
    # Note: this count is local-only. The remote may have more files
    # that haven't been pulled yet. /sync pulls before scanning.
    local_hint = " (本地; remote 可能更多)"
    if hard:
        return (
            Cue(
                key="zettelm",
                severity="hard",
                command_path=".claude/commands/sync.md",
                message=(
                    f"zettelm 有 {n} 条待 digest{local_hint} (最老 {oldest_age_days} 天). "
                    f"建议先跑 `/sync` 把内容归位再继续. 现在跑吗?"
                ),
            ),
            f"n={n} oldest_age={oldest_age_days}; hard floor",
        )

    return (
        Cue(
            key="zettelm",
            severity="soft",
            command_path=".claude/commands/sync.md",
            message=f"提示: zettelm 有 {n} 条待 digest{local_hint}. 想现在跑 `/sync`?",
        ),
        f"n={n} oldest_age={oldest_age_days}; soft cue",
    )


def check_recurring(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Recurring obligations cue.

    Fires when one or more recurring items in $OV/gtd/recurring.md are overdue
    (today > last-done + every) or due-soon (within 7 days). Severity escalates
    to `hard` when any item is overdue by more than 30 days — a 100-day-overdue
    health/maintenance task should not register softer than a 7-day-old
    zettelm capture.
    """
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
    """Self-declared aggregate trackers lagging their subject SOT.

    Fires when `aggregate_freshness.py --discover --stale-only` reports one
    or more stale aggregates. Soft cue: the divergence is advisory, the
    user may still want to read the aggregate, but should know it's stale
    before quoting it.
    """
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


def check_routine_outputs(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Unreviewed outputs from remote cron routines.

    Vault-agnostic mechanism: reads `$OV/_meta/routine_watch.toml` to learn
    which output directories belong to which routine. Each routine entry
    declares its `output_dir`, `file_pattern`, and human `label`. User policy
    lives in the TOML; this function is the engine.

    Ack mechanism: `$OV/_meta/routine_acks.json` stores `{output_dir: last_acked_filename}`.
    Cue fires when a directory's latest file (sorted by filename) > acked filename.
    User mutes by updating that JSON after reading a report.
    """
    import json

    config_path = _meta_dir(ov) / "routine_watch.toml"
    if not config_path.is_file():
        return None, "_meta/routine_watch.toml missing; skip"

    try:
        config = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return None, f"routine_watch.toml parse failed: {exc!r}"

    routines = config.get("routine", [])
    if not routines:
        return None, "no routines declared in routine_watch.toml"

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
        pattern = r.get("file_pattern", "*")
        label = r.get("label", r.get("name", "?"))
        if not output_dir:
            debug_parts.append(f"{label}: missing output_dir")
            continue
        d = ov / output_dir
        if not d.is_dir():
            debug_parts.append(f"{label}: dir missing")
            continue
        files = sorted(d.glob(pattern), key=lambda p: p.name)
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
    """Policy compliance for remote-routine $OV-persistence.

    Per `protocols/remote-routines.md` § Policy, every routine MUST persist
    canonical output to $OV. Each routine entry in
    `$OV/_meta/routine_watch.toml` should declare either:
      - `drive_write_enforced = true`  (compliant), OR
      - `needs_drive_write_update = true`  (acknowledged migration debt)
    A routine missing both flags violates the policy without acknowledgment.
    Surfaces the count of non-compliant routines as a soft cue.
    """

    config_path = _meta_dir(ov) / "routine_watch.toml"
    if not config_path.is_file():
        return None, "no routine_watch.toml; skip"
    try:
        config = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return None, f"toml parse failed: {exc!r}"
    routines = config.get("routine", [])
    if not routines:
        return None, "no routines declared"
    violators: list[str] = []
    for r in routines:
        # Local routines write to $OV directly via the filesystem; the Drive-write
        # policy applies only to remote (claude.ai) routines that persist over MCP.
        # Per protocols/remote-routines.md § routine_watch.toml: local entries
        # carry no drive_write_enforced flag.
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
                f"`$OV/_meta/routine_watch.toml`."
            ),
        ),
        f"violators={len(violators)}/{len(routines)}; soft cue",
    )


def _local_owner_start_date(ov: Path, config: dict) -> date | None:
    """Return the current local-routine ownership epoch, when configured."""
    coordination = config.get("coordination", {})
    if not isinstance(coordination, dict) or coordination.get("backend") != "owner":
        return None
    owner_path = _meta_dir(ov) / "routine_owner.toml"
    try:
        owner = tomllib.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    transferred = owner.get("transferred_at")
    if isinstance(transferred, datetime):
        value = transferred
    elif isinstance(transferred, str):
        try:
            value = datetime.fromisoformat(transferred)
        except ValueError:
            return None
    else:
        return None
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.date()


def _local_owner_label(ov: Path, config: dict) -> str | None:
    """Return the machine label that owns local routines, when configured."""
    coordination = config.get("coordination", {})
    if not isinstance(coordination, dict) or coordination.get("backend") != "owner":
        return None
    owner_path = _meta_dir(ov) / "routine_owner.toml"
    try:
        owner = tomllib.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    label = owner.get("owner_label")
    if not isinstance(label, str) or not label:
        return None
    # owner_label is a snapshot of the hostname taken once at claim time and
    # never refreshed, while the claim writer records the live hostname. If the
    # two ever diverge, filtering on the label would drop every record and
    # report the whole fleet as missed. Only filter on a label the records
    # actually use; otherwise fail open to the pre-filter behavior.
    runs = _meta_dir(ov) / "routine_runs"
    if not runs.is_dir():
        return None
    for routine_dir in runs.iterdir():
        if not routine_dir.is_dir():
            continue
        for path in routine_dir.glob("*.toml"):
            try:
                claim = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if claim.get("machine") == label:
                return label
    return None


def _latest_local_claim(
    ov: Path,
    routine: str,
    *,
    not_before: date | None = None,
    machine: str | None = None,
) -> tuple[date, dict, Path] | None:
    """Load the latest dated claim for one local routine.

    Under the owner backend a machine that lost ownership can still write run
    records from a stale checkout. Those are not evidence that the routine ran,
    so `machine` restricts the search to the owning label.
    """
    routine_dir = _meta_dir(ov) / "routine_runs" / routine
    if not routine_dir.is_dir():
        return None
    candidates: list[tuple[date, Path]] = []
    for path in routine_dir.glob("*.toml"):
        try:
            claim_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if not_before is None or claim_date >= not_before:
            candidates.append((claim_date, path))
    for claim_date, path in sorted(candidates, reverse=True):
        try:
            claim = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        claim_machine = claim.get("machine")
        if machine is not None and claim_machine is not None:
            if not isinstance(claim_machine, str) or claim_machine != machine:
                continue
        if claim.get("contract_version") == 2:
            try:
                validate_claim(
                    claim,
                    routine=routine,
                    cycle=path.stem,
                    allow_legacy_owner_generation=True,
                )
            except ValueError:
                continue
        return claim_date, claim, path
    return None


_cron_fields = cron_spec.cron_fields
_cron_field_matches = cron_spec.cron_field_matches
_scheduled_dates = cron_spec.scheduled_dates


def check_routine_staleness(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Detect routines that fire but produce no output.

    For each routine in routine_watch.toml, estimates expected cadence from
    the cron field, then checks whether the latest output file is older than
    cadence + tolerance. Catches silent Drive-write failures that
    check_routine_outputs (which only reports *new* files) cannot see.
    """

    config_path = _meta_dir(ov) / "routine_watch.toml"
    if not config_path.is_file():
        return None, "_meta/routine_watch.toml missing; skip"

    try:
        config = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return None, f"routine_watch.toml parse failed: {exc!r}"

    routines = config.get("routine", [])
    if not routines:
        return None, "no routines declared"

    owner_start = _local_owner_start_date(ov, config)
    owner_label = _local_owner_label(ov, config)
    stale: list[str] = []
    debug_parts: list[str] = []

    for r in routines:
        name = r.get("name", "?")
        label = r.get("label", r.get("name", "?"))
        output_dir = r.get("output_dir")
        pattern = r.get("file_pattern", "*")
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
        latest_claim = (
            _latest_local_claim(
                ov, str(name), not_before=owner_start, machine=owner_label
            )
            if is_local
            else None
        )

        d = ov / output_dir
        if not d.is_dir():
            if (
                is_local
                and owner_start is not None
                and latest_claim is None
                and (today - owner_start).days <= threshold
            ):
                debug_parts.append(
                    f"{label}: dir missing inside owner grace ({owner_start})"
                )
                continue
            stale.append(f"{label} (output dir missing)")
            debug_parts.append(f"{label}: dir missing; cadence={cadence_days}d")
            continue

        files = sorted(d.glob(pattern), key=lambda p: p.name)
        if not files:
            if (
                is_local
                and owner_start is not None
                and latest_claim is None
                and (today - owner_start).days <= threshold
            ):
                debug_parts.append(
                    f"{label}: no files inside owner grace ({owner_start})"
                )
                continue
            stale.append(f"{label} (no output files)")
            debug_parts.append(f"{label}: no files; cadence={cadence_days}d")
            continue

        latest_name = files[-1].name
        latest_date = _extract_date_from_filename(latest_name)
        if latest_date is None:
            debug_parts.append(f"{label}: can't parse date from {latest_name}")
            continue

        if latest_claim is not None:
            claim_date, claim, _claim_path = latest_claim
            if claim.get("status") == "completed" and claim_date > latest_date:
                stale.append(
                    f"{label} (completed claim {claim_date} newer than output {latest_date})"
                )
                debug_parts.append(
                    f"{label}: completed claim={claim_date} > output={latest_date}"
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
            command_path="_meta/routine_watch.toml",
            message=(
                f"{len(stale)} routine(s) with missing/stale output: {listing}. "
                f"Check the active scheduler in routine_watch.toml, then inspect its "
                f"local claim/diagnostic or cloud session and connector logs."
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
    """Detect routines with intermittent output failures.

    Complements check_routine_staleness (which catches total outages) by
    counting actual vs expected output files over a lookback window. A daily
    routine that succeeds every other day never triggers staleness, but its
    hit rate is 50% and should surface.

    Lookback window: max(14, 3 * cadence) days. This gives enough samples
    for statistical signal while staying recent enough to reflect current
    reliability. Fires when hit rate drops below 70%.

    Only evaluates routines with cadence <= 7 days; longer-cadence routines
    (monthly, quarterly) don't accumulate enough samples for hit-rate math
    and are adequately covered by check_routine_staleness.
    """

    config_path = _meta_dir(ov) / "routine_watch.toml"
    if not config_path.is_file():
        return None, "_meta/routine_watch.toml missing; skip"

    try:
        config = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return None, f"routine_watch.toml parse failed: {exc!r}"

    routines = config.get("routine", [])
    if not routines:
        return None, "no routines declared"

    now = now or datetime.now().astimezone()
    owner_start = _local_owner_start_date(ov, config)
    degraded: list[str] = []
    debug_parts: list[str] = []

    for r in routines:
        label = r.get("label", r.get("name", "?"))
        output_dir = r.get("output_dir")
        pattern = r.get("file_pattern", "*")
        cron = r.get("cron", "")
        if not output_dir or not cron:
            continue

        cadence_days = _estimate_cadence_days(cron)
        if cadence_days is None or cadence_days > 7:
            debug_parts.append(f"{label}: cadence={cadence_days}d; skip hitrate")
            continue

        d = ov / output_dir
        if not d.is_dir():
            continue  # staleness cue handles this

        max_lookback = max(14, 3 * cadence_days)
        cutoff = today - timedelta(days=max_lookback - 1)

        files = sorted(d.glob(pattern), key=lambda p: p.name)
        dated_files: list[date] = []
        for f in files:
            fd = _extract_date_from_filename(f.name)
            if fd is not None:
                dated_files.append(fd)

        if not dated_files:
            continue  # staleness cue handles this

        # Cap lookback to oldest file date so new routines aren't penalized
        # for not existing before their first output. Local routines also start
        # at the current owner's transfer epoch.
        oldest_file = min(dated_files)
        effective_start = max(cutoff, oldest_file)
        if r.get("execution") == "local" and owner_start is not None:
            effective_start = max(effective_start, owner_start)
        effective_lookback = (today - effective_start).days + 1

        scheduled = _scheduled_dates(cron, effective_start, now)
        expected_dates = set(scheduled)
        recent_dates = {fd for fd in dated_files if effective_start <= fd <= today}
        expected = len(expected_dates)
        if expected < 3:
            debug_parts.append(
                f"{label}: expected={expected} in {effective_lookback}d; too few samples"
            )
            continue

        actual = len(recent_dates & expected_dates)
        rate = actual / expected if expected > 0 else 1.0

        if rate < 0.70:
            pct = int(rate * 100)
            degraded.append(f"{label} ({actual}/{expected} runs, {pct}%)")
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
            command_path="_meta/routine_watch.toml",
            message=(
                f"{len(degraded)} routine(s) with degraded output rate: {listing}. "
                f"The active scheduler is firing but output is intermittent. "
                f"Inspect local claim/diagnostic or cloud session and connector logs."
            ),
        ),
        f"degraded={len(degraded)}; {debug}",
    )


_estimate_cadence_days = cron_spec.estimate_cadence_days


def _extract_date_from_filename(name: str) -> date | None:
    """Extract YYYY-MM-DD from a filename prefix or embedded pattern."""
    import re as _re

    m = _re.search(r"(\d{4}-\d{2}-\d{2})", name)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def check_autoevo_pending(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Pending autoevo decisions awaiting human triage.

    `/autoevo-nightly` writes uncertain Forgetter findings to
    `$OV/_meta/autoevo_pending.toml` (status = "pending"). This cue
    surfaces them at session start with a per-category breakdown so the
    user can run `/autoevo-review` to triage. Per `protocols/autoevo.md`.

    Severity is `soft` by default. Escalates to `hard` when any of:
    - any entry's `proposed_at` is more than 14 days ago
    - any entry's `proposed_at` failed to parse (corrupt_dates > 0); a
      queue with bad timestamps can hide arbitrarily old entries
    - any entry's `surface_count >= 3` (the auto-dismiss threshold from
      `/autoevo-review`; surface before the entry is silently swept)

    Stays silent when the queue file is missing, empty, or all entries
    are already resolved (status != "pending").
    """

    config_path = _meta_dir(ov) / "autoevo_pending.toml"
    if not config_path.is_file():
        return None, "_meta/autoevo_pending.toml missing; skip"

    try:
        config = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        # A queue file that exists but cannot be parsed is the worst-of-both:
        # /autoevo-review will refuse to operate, /autoevo-nightly's queue
        # append will likely also fail, and silent return here would leave
        # the user with no signal at all. Fire a hard cue routing the user
        # to repair the file by hand.
        return (
            Cue(
                key="autoevo_pending",
                severity="hard",
                command_path="_meta/autoevo_pending.toml",
                message=(
                    f"Autoevo pending queue file is corrupted "
                    f"(`_meta/autoevo_pending.toml`): {type(exc).__name__}. "
                    f"`/autoevo-review` and `/autoevo-nightly` queue ops cannot proceed. "
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
        # surface_count >= 3 is the auto-dismiss threshold per
        # protocols/autoevo.md § Pending queue. If any entry has been
        # repeatedly skipped, escalate so the user sees them before /autoevo-review
        # auto-dismisses them on its next run.
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
            command_path=".claude/commands/autoevo-review.md",
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
    """Catches silent nightly-bot failures AND surfaces skipped runs.

    `/autoevo-nightly` writes `<paths.agent_findings>/autoevo-applied-<RUN_DATE>.md`
    on every run — even when a pre-flight gate aborts (the Skipped section
    is populated). The bot fires at 05:00 local, so RUN_DATE is today, and
    this cue (gated to fire after 06:00) inspects today's audit file. Two
    failure modes to surface:

    1. **Audit file missing.** The bot did not run at all (launchd auth
       failed, $OV unset, claude CLI missing, etc.). Soft cue pointing at
       the launchd README.
    2. **Latest attempt has a non-empty Skipped / Errors section.** The bot
       ran but its latest pre-flight or sweep attempt did not complete.
       Earlier same-day skips are superseded by a later clean retry.

    Stays silent when:
    - The agent-findings dir doesn't exist yet (fresh vault, bot never ran).
    - Today's audit file exists AND its Skipped/Errors sections are
      empty or contain only "(none)".
    - It is earlier than 06:00 local today (the 5am bot might still be running).

    Soft cue by default.
    """
    # Don't fire before 06:00 local — bot is given a full hour to complete.
    now = now or datetime.now()
    if now.hour < 6:
        return None, "before 06:00 local; skip"

    findings_dir = tier("agent_findings")
    if not findings_dir.is_dir():
        return None, "agent_findings dir missing; bot never installed"

    # The bot runs at 05:00 local and writes its audit log with today's
    # RUN_DATE. After 06:00 today, today's audit file should exist.
    expected_name = f"autoevo-applied-{today.isoformat()}.md"
    # `agent-findings/` is a fission-eligible tier: resolve today's audit
    # anywhere under it, not only at the tier root.
    matches = tier_files("agent_findings", expected_name)
    expected_path = matches[-1] if matches else findings_dir / expected_name

    def rel(path: Path) -> str:
        for base in (ov, ov.resolve()):
            try:
                return str(path.relative_to(base))
            except ValueError:
                continue
        return path.name

    # Branch 1: audit file missing entirely.
    if not expected_path.is_file():
        # If NO audit log exists at all under the dir, the bot was probably
        # never installed yet — stay silent rather than nag a user who hasn't
        # set it up.
        any_audit = list(findings_dir.rglob("autoevo-applied-*.md"))
        if not any_audit:
            return None, "no audit logs ever; bot not installed yet"
        # The runner's claim file knows why no audit was written (a crash
        # before the audit step). Surface its status and error instead of a
        # generic cause list so the fix is visible without reading /tmp logs.
        claim_path = _meta_dir(ov) / "routine_runs" / "autoevo-nightly" / f"{today.isoformat()}.toml"
        claim_hint = ""
        if claim_path.is_file():
            try:
                claim = tomllib.loads(claim_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                claim = {}
            status = claim.get("status")
            error = claim.get("error")
            if status:
                claim_hint = f" Claim status: `{status}`"
                if error:
                    claim_hint += f" (`{error}`)"
                claim_hint += "; last stderr lines are in `/tmp/com.atelier.autoevo-nightly.err`."
        return (
            Cue(
                key="autoevo_ran",
                severity="soft",
                command_path="scripts/launchd/README.md",
                message=(
                    f"Nightly autoevo did not run today ({today.isoformat()}).{claim_hint} "
                    f"If no claim exists, check `~/Library/LaunchAgents/com.atelier.autoevo-nightly.plist` "
                    f"($OV unset in launchd shell, expired credentials, unloaded LaunchAgent). "
                    f"A sleeping Mac should catch up on wake."
                ),
            ),
            f"expected {expected_name} missing",
        )

    # Branch 2: inspect only the latest attempt. The wake/retry schedule may
    # produce an early blocked run followed by a successful same-day sweep.
    # Keeping the whole-day "any skip" rule would preserve a stale warning
    # after the later attempt had already recovered.
    try:
        body = expected_path.read_text()
    except OSError as exc:
        return None, f"audit file unreadable: {exc!r}"

    import re

    attempt_starts = list(
        re.finditer(r"^##\s+(?:Autoevo Run|Run)\b", body, re.MULTILINE)
    )
    latest_body = body[attempt_starts[-1].start() :] if attempt_starts else body

    # Stop at the next heading so a Skipped section body does not accidentally
    # include the immediately following Errors heading.
    def section_populated(text: str, heading: str) -> bool:
        pat = rf"^###\s+{re.escape(heading)}.*?\n(.*?)(?=^###|^##|\Z)"
        for m in re.finditer(pat, text, re.MULTILINE | re.DOTALL):
            for raw in m.group(1).splitlines():
                line = raw.strip()
                if not line or line in ("(none)", "- (none)"):
                    continue
                return True
        return False

    skipped = section_populated(latest_body, "Skipped")
    errored = section_populated(latest_body, "Errors")

    if not skipped and not errored:
        return None, f"today's audit log clean ({expected_name})"

    parts: list[str] = []
    if skipped:
        parts.append("Skipped section populated")
    if errored:
        parts.append("Errors section populated")
    listing = " and ".join(parts)

    # Escalation: the hourly retry schedule cannot clear a blocker that needs
    # a human (dirty sweep scope, stale credentials, missing index). When the
    # same gate has blocked the latest attempt for several consecutive days,
    # stop whispering and name the fix; a soft cue let one gate block the
    # bot for months.
    def latest_gate(text: str) -> str | None:
        starts = list(re.finditer(r"^##\s+(?:Autoevo Run|Run)\b", text, re.MULTILINE))
        latest = text[starts[-1].start() :] if starts else text
        m = re.search(
            r"^###\s+Skipped.*?\n(.*?)(?=^###|^##|\Z)", latest, re.MULTILINE | re.DOTALL
        )
        if not m:
            return None
        for raw in m.group(1).splitlines():
            line = raw.strip().lstrip("- ").strip()
            if line and line != "(none)":
                return line.split(":", 1)[0].strip()
        return None

    gate = latest_gate(latest_body)
    streak = 0
    if gate:
        streak = 1
        for back in range(1, 30):
            prior_name = f"autoevo-applied-{(today - timedelta(days=back)).isoformat()}.md"
            prior_matches = tier_files("agent_findings", prior_name)
            if not prior_matches:
                break
            prior = prior_matches[-1]
            try:
                if latest_gate(prior.read_text()) != gate:
                    break
            except OSError:
                break
            streak += 1

    # Keys MUST match the `gate` strings `autoevo_preflight.py` emits;
    # tests/test_cues.py pins every key against that file's source.
    gate_fixes = {
        "dirty_autoevo_state": (
            "uncommitted `_meta/autoevo_*.toml`; commit or restore the queue "
            "state, then check `uv run scripts/autoevo_preflight.py --dirty-scope`"
        ),
        "dirty_zettelm_worktree": "finish or commit the mobile-capture digest in `<paths.zettelm>/`",
        "session_lock_unreadable": "the session-lock file's metadata cannot be read; check `<paths.cache>/atelier-session-lock` permissions and disk health",
        "session_active": "a stale `<paths.cache>/atelier-session-lock`; remove it if no session is open",
        "git_index_lock_present": "a stale `.git/index.lock` in $OV; remove it only if no git process is running",
        "git_operation_in_progress": "a merge, rebase, cherry-pick, or bisect is in progress in $OV; finish or abort it (`git status` says which) before the bot can commit",
        "git_index_missing": "the $OV git index is missing; restore it before the bot can classify files",
        "git_not_worktree": "$OV is not a git worktree; re-init or fix the mount before the bot can commit",
        "privacy_hits": "resolve the privacy_check.py finding in $OV",
        "semantic_unavailable": "rebuild the semantic index or restore the cached model snapshot",
        "environment_unavailable": "storage or git timeouts; check Drive sync health, then let the hourly retry clear it",
        "audit_recovery_deferred": "an unrecovered bot audit; check `<paths.cache>/autoevo-preflight-owned-audit.json` readability",
    }
    if gate and streak >= 3:
        fix = gate_fixes.get(gate, "see the audit file and scripts/launchd/README.md")
        return (
            Cue(
                key="autoevo_ran",
                severity="hard",
                command_path=rel(expected_path),
                message=(
                    f"Nightly autoevo has been blocked by `{gate}` for {streak} consecutive days; "
                    f"hourly retries cannot clear it. Fix: {fix}."
                ),
            ),
            f"blocker {gate} streak={streak}; escalated to hard",
        )

    return (
        Cue(
            key="autoevo_ran",
            severity="soft",
            command_path=rel(expected_path),
            message=(
                f"Today's latest autoevo attempt has issues: {listing}. "
                f"Read `{rel(expected_path)}` for the audit details."
            ),
        ),
        f"audit log present but {listing}",
    )


def _recap_local_runs(ov: Path, today: date, verbose: bool = False) -> list[str]:
    """One-liner recaps of recent local routine runs (informational, not cues).

    Reads claim files from `$OV/_meta/routine_runs/*/` for today and yesterday.
    For completed runs, peeks at the corresponding audit log (if any) to extract
    counts. Returns a list of human-readable recap lines.
    """

    runs_dir = _meta_dir(ov) / "routine_runs"
    if not runs_dir.is_dir():
        return []

    recaps: list[str] = []
    yesterday = today - __import__("datetime").timedelta(days=1)

    for routine_dir in sorted(runs_dir.iterdir()):
        if not routine_dir.is_dir():
            continue
        routine_name = routine_dir.name

        for check_date in [today, yesterday]:
            claim = routine_dir / f"{check_date.isoformat()}.toml"
            if not claim.is_file():
                continue
            try:
                data = tomllib.loads(claim.read_text())
            except Exception:
                continue
            if data.get("contract_version") == 2:
                try:
                    validate_claim(
                        data,
                        routine=routine_name,
                        cycle=check_date.isoformat(),
                        allow_legacy_owner_generation=True,
                    )
                except ValueError:
                    continue

            status = data.get("status", "unknown")
            machine = data.get("machine", "?")
            duration = data.get("duration_seconds")

            if status != "completed":
                continue

            summary = data.get("result_summary", "")
            if not summary:
                summary = _extract_audit_summary(ov, routine_name, check_date)

            dur_str = f" ({duration}s)" if duration else ""
            date_str = "today" if check_date == today else "yesterday"
            recap = f"{routine_name} ran {date_str} on {machine}{dur_str}"
            if summary:
                recap += f": {summary}"
            recaps.append(recap)
            break  # only show the most recent per routine

    if verbose and recaps:
        for r in recaps:
            print(f"# debug: recap: {r}", file=sys.stderr)

    return recaps


def _extract_audit_summary(ov: Path, routine_name: str, run_date: date) -> str:
    """Extract a short summary from an autoevo audit log."""
    import re

    if routine_name != "autoevo-nightly":
        return ""

    findings_dir = tier("agent_findings")
    if not findings_dir.is_dir():
        return ""

    audit = findings_dir / f"autoevo-applied-{run_date.isoformat()}.md"
    if not audit.is_file():
        return ""

    try:
        body = audit.read_text(errors="replace")
    except OSError:
        return ""

    counts: dict[str, int] = {}
    for heading in ("Auto-applied", "Logged to pending queue", "Skipped", "Errors"):
        pat = rf"^###\s+{re.escape(heading)}\s*\((\d+)\)"
        m = re.search(pat, body, re.MULTILINE)
        if m:
            counts[heading.split()[0].lower()] = int(m.group(1))
            continue
        # Count bullet lines under the heading.
        sect_pat = rf"^###\s+{re.escape(heading)}.*?\n(.*?)(?=^###|^##|\Z)"
        sect_m = re.search(sect_pat, body, re.MULTILINE | re.DOTALL)
        if sect_m:
            bullets = [
                ln
                for ln in sect_m.group(1).splitlines()
                if ln.strip().startswith("- ") and ln.strip() not in ("- (none)",)
            ]
            if bullets:
                counts[heading.split()[0].lower()] = len(bullets)

    if not counts:
        return ""

    parts = [f"{k}={v}" for k, v in counts.items()]
    return ", ".join(parts)


def _claim_failure_reason(claim_data: dict) -> str:
    """The claim's own account of why a cycle failed.

    Worth surfacing because the fuller transcript is already gone by the time
    anyone reads the cue: every routine plist sends the runner's output to
    /tmp, which macOS purges. `error` is the coarse phase; `error_detail` is the
    screened tail of what actually happened. Prefer the detail, fall back to the
    phase, and say nothing rather than guess.
    """
    detail = str(claim_data.get("error_detail") or "").strip()
    error = str(claim_data.get("error") or "").strip()
    if detail and error and not detail.startswith(error):
        return f"{error}: {detail}"
    return detail or error


def check_local_routine_missed(
    ov: Path,
    today: date,
    *,
    now: datetime | None = None,
) -> tuple[Cue | None, str]:
    """Detect local routines that missed their scheduled run.

    Reads `$OV/_meta/routine_watch.toml` for routines with `execution = "local"`.
    For each, computes the latest cron occurrence that is already due, then
    checks the corresponding local claim. Ownership transfer time is the
    earliest eligible schedule date, so a newly assigned machine is not
    blamed for historical cycles.

    Gated to fire after 06:00 local so the routine has time to complete.
    Stays silent when no local routines are declared or `routine_runs/` is absent
    (bot never installed).
    """

    now = now or datetime.now().astimezone()
    if now.hour < 6:
        return None, "before 06:00 local; skip"

    config_path = _meta_dir(ov) / "routine_watch.toml"
    if not config_path.is_file():
        return None, "_meta/routine_watch.toml missing; skip"

    try:
        config = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return None, f"routine_watch.toml parse failed: {exc!r}"

    routines = [r for r in config.get("routine", []) if r.get("execution") == "local"]
    if not routines:
        return None, "no local routines declared"

    owner_start = _local_owner_start_date(ov, config)
    owner_label = _local_owner_label(ov, config)
    runs_dir = _meta_dir(ov) / "routine_runs"
    if not runs_dir.is_dir():
        return None, "routine_runs/ absent; never installed"

    missed: list[str] = []
    debug_parts: list[str] = []

    for r in routines:
        name = r.get("name", "?")
        label = r.get("label", name)
        cron = r.get("cron", "")
        routine_dir = runs_dir / name
        cadence_days = _estimate_cadence_days(cron)
        if cadence_days is None:
            debug_parts.append(f"{label}: unparseable cron")
            continue

        # One day of grace after an ownership transfer.
        #
        # Claims are filtered to the owning machine, so on the day ownership
        # moves, every cycle the *previous* owner already completed looks like a
        # cycle this machine never ran. Measured on 2026-08-31: a transfer at
        # 21:11 local made five routines report "no claim" for occurrences the
        # previous owner had completed fifteen hours earlier. Blaming the new
        # owner for those is worse than staying quiet for a day, because a cue
        # that cries wolf on every transfer stops being read.
        schedule_start = owner_start or (
            today - timedelta(days=max(366, 3 * cadence_days))
        )
        if owner_start is not None:
            schedule_start = max(schedule_start, owner_start + timedelta(days=1))
        due_dates = _scheduled_dates(cron, schedule_start, now)
        if not due_dates:
            debug_parts.append(
                f"{label}: no scheduled occurrence due since {schedule_start}"
            )
            continue
        expected_date = due_dates[-1]

        if not routine_dir.is_dir():
            if owner_start is not None:
                missed.append(f"{label} (no claim for {expected_date})")
                debug_parts.append(f"{label}: no runs dir; expected={expected_date}")
            else:
                debug_parts.append(f"{label}: no runs dir; installation unknown")
            continue

        latest = _latest_local_claim(
            ov, str(name), not_before=owner_start, machine=owner_label
        )
        if latest is None:
            any_past = list(routine_dir.glob("*.toml"))
            if any_past or owner_start is not None:
                missed.append(f"{label} (no claim for {expected_date})")
                debug_parts.append(
                    f"{label}: expected={expected_date}; no eligible claim"
                )
            else:
                debug_parts.append(f"{label}: never ran; skip")
            continue

        claim_date, claim_data, claim_path = latest
        if claim_date < expected_date:
            missed.append(f"{label} (no claim for {expected_date})")
            debug_parts.append(
                f"{label}: latest={claim_date}; expected={expected_date}"
            )
            continue

        status = claim_data.get("status", "unknown")
        if status == "completed":
            debug_parts.append(f"{label}: {claim_date} completed")
        elif status == "running":
            claimed_value = claim_data.get("claimed_at")
            try:
                claimed_at = datetime.fromisoformat(str(claimed_value))
            except ValueError:
                claimed_at = datetime.fromtimestamp(claim_path.stat().st_mtime)
            comparison_now = (
                now.astimezone(claimed_at.tzinfo)
                if claimed_at.tzinfo is not None
                else now.replace(tzinfo=None)
            )
            age_hours = (comparison_now - claimed_at).total_seconds() / 3600
            if age_hours >= 6:
                missed.append(
                    f"{label} (running stale {int(age_hours)}h on {claim_date})"
                )
                debug_parts.append(
                    f"{label}: {claim_date} running stale {age_hours:.1f}h"
                )
            else:
                debug_parts.append(
                    f"{label}: {claim_date} still running {age_hours:.1f}h"
                )
        elif status == "deferred":
            missed.append(f"{label} (deferred on {claim_date}; retry scheduled)")
            debug_parts.append(f"{label}: {claim_date} deferred")
        elif status in {"failed", "completion-uncertain", "retry-approved"}:
            reason = _claim_failure_reason(claim_data)
            suffix = f": {reason[:120]}" if reason else ""
            missed.append(f"{label} ({status} on {claim_date}{suffix})")
            debug_parts.append(f"{label}: {claim_date} {status} {reason[:200]}")
        else:
            missed.append(f"{label} (unknown claim status on {claim_date})")
            debug_parts.append(f"{label}: {claim_date} unknown status={status}")

    debug = "; ".join(debug_parts)
    if not missed:
        return None, debug

    listing = "; ".join(missed[:3])
    if len(missed) > 3:
        listing += f", +{len(missed) - 3} more"

    return (
        Cue(
            key="local_routine_missed",
            severity="soft",
            command_path="scripts/launchd/README.md",
            message=(
                f"{len(missed)} local routine(s) missed: {listing}. "
                f"Common causes: machine asleep, launchd not loaded, expired credentials. "
                f"Deferred routines retry at their next trigger. For failed or uncertain "
                f"cycles, check `scripts/launchd/README.md`, review effects, and use guarded "
                f"cycle recovery before retrying an existing failed, uncertain, or "
                f"stale-running claim."
            ),
        ),
        f"missed={len(missed)}; {debug}",
    )


def check_career_growth(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Sunday growth review against the current career plan.

    Reviews the past week's learning, engineering output, forward-looking
    design work, and public contributions. The actual goals and cadence stay
    in the private plan rather than being copied into this public script.

    Fires (soft) when:
      - today is Sunday (weekday 6) AND the last growth-review is >=6 days old
        (or none exists yet), OR
      - it's been >9 days since the last growth-review (catches a missed Sunday
        on whatever weekday the next session lands).

    Goes silent once a `reflections/YYYY-MM-DD-growth-review.md` exists for the
    current week. Stays silent entirely if no career plan is present. Snooze:
    `cues.py snooze career_growth [--days N]`.
    """
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


# Registry. To add a new cue, append a `check_*` function above and
# register it here.


def check_eval_regression(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Compare the two most recent eval snapshots; cue on a route-coverage drop.

    The eval harness exists so evolution is measured; a snapshot that scores
    below its predecessor must reach the user, not sit in a directory. The
    routing score is the share of `/hi` routes that landed on a catalog row
    with confidence (see scripts/eval_run.py).
    """
    import re

    evals_dir = _meta_dir(ov) / "evals"
    if not evals_dir.is_dir():
        return None, "no evals recorded; skip"
    # scripts/eval_run.py writes `<date>-<sha>.json`. The directory also holds
    # working files (routing_cases.json), and those sort AFTER every dated name,
    # so a bare glob would always pick one as the newer snapshot and the cue
    # could never fire.
    dated = re.compile(r"^\d{4}-\d{2}-\d{2}-[^/]+\.json$")
    snapshots = sorted(s for s in evals_dir.glob("*.json") if dated.match(s.name))[-2:]
    if len(snapshots) < 2:
        return None, f"{len(snapshots)} dated snapshot(s); need 2 to compare"
    try:
        prev, curr = (json.loads(s.read_text(encoding="utf-8")) for s in snapshots)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"snapshot unreadable: {exc!r}"
    tracked = (
        ("route coverage", lambda snap: snap.get("routing") or {}),
        ("judged routing", lambda snap: (snap.get("judged") or {}).get("routing") or {}),
    )
    drops: list[str] = []
    incomparable: list[str] = []
    seen = 0
    for label, pick in tracked:
        p_block, c_block = pick(prev), pick(curr)
        p_score, c_score = p_block.get("score"), c_block.get("score")
        if not isinstance(p_score, (int, float)) or not isinstance(c_score, (int, float)):
            continue
        # A snapshot predating the metric label measured something else under the
        # same key. Comparing across metrics manufactures a regression instead of
        # reporting one.
        if p_block.get("metric") != c_block.get("metric"):
            incomparable.append(
                f"{label} ({p_block.get('metric') or 'unlabelled'} vs {c_block.get('metric') or 'unlabelled'})"
            )
            continue
        seen += 1
        if c_score < p_score:
            drops.append(f"{label} {p_score:.0%} -> {c_score:.0%}")
    if seen == 0:
        if incomparable:
            return None, f"no comparable routing scores; metric changed: {'; '.join(incomparable)}"
        return None, "no comparable routing scores"
    if not drops:
        return None, f"{seen} routing metric(s) held; no regression"
    return (
        Cue(
            key="eval_regression",
            severity="hard",
            command_path="scripts/eval_run.py",
            message=(
                f"Eval regression: {'; '.join(drops)} "
                f"({snapshots[0].name} -> {snapshots[1].name}). "
                f"看 `{snapshots[1].name}` 里的 misses, 补 catalog 或有意接受后重跑 eval."
            ),
        ),
        "; ".join(drops),
    )


def check_intent_misses(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Catalog coverage feedback: a recurring unrouted `/hi` request needs a row.

    Reads the route ledger (`intent_routes/`) through the same reader and
    aggregation as `intent-misses`, so the cue fires exactly when that review
    would propose something: a phrase unrouted on 3+ distinct days in 14.
    """
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


def check_meta_reflection_due(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Every 5th session log in the trailing 30 days: meta-reflection is due.

    The protocol said "after every 5th session" with no counter anywhere;
    38 logs accumulated and zero meta-reflections ran. The headless
    `meta-reflection-draft` routine (protocols/meta-reflection.md § Headless
    draft) now writes the draft; when one exists in the trailing 30 days this
    cue stays silent and the review-debt cue carries it. It fires only when
    the draft is missing, which means the routine is not installed or failed.
    """
    sessions = ov / tier_segments().get("sessions", "sessions")
    if not sessions.is_dir():
        return None, "no session logs; skip"
    cutoff = today - timedelta(days=30)
    recent = 0
    for path in sessions.rglob("*.md"):
        try:
            if date.fromisoformat(path.name[:10]) >= cutoff:
                recent += 1
        except ValueError:
            continue
    if recent == 0 or recent % 5 != 0:
        return None, f"{recent} session logs in 30d; not a positive multiple of 5"
    drafts_dir = ov / tier_segments().get("agent_findings", "agent-findings") / "meta-reflection"
    for path in sorted(drafts_dir.glob("*-meta-reflection-draft.md"), reverse=True):
        try:
            if date.fromisoformat(path.name[:10]) >= cutoff:
                return None, f"{recent} logs in 30d; draft {path.name} exists, review-debt cue owns it"
        except ValueError:
            continue
    return (
        Cue(
            key="meta_reflection",
            severity="soft",
            command_path="protocols/meta-reflection.md",
            message=(
                f"最近 30 天已积累 {recent} 个 session logs, 但没有 meta-reflection 草稿. "
                f"检查 `meta-reflection-draft` 例程 (protocols/meta-reflection.md § Headless draft), "
                f"或手动跑 (`uv run scripts/session_stats.py` 先看数据)."
            ),
        ),
        f"{recent} logs in 30d; multiple of 5; no draft",
    )


def check_routine_failures(ov: Path, today: date) -> tuple[Cue | None, str]:
    """Surface pre-claim failures, which no other cue can see.

    `routine_runner.sh` writes a diagnostic into
    `$OV/_meta/routine_failures/<routine>/` when a cycle dies *before* the claim
    exists: the owner probe errored, capability preflight failed, the lock could
    not be acquired. Every other routine cue reads claims, so this entire class
    of failure was written to disk and never read by anything. A routine can
    fail this way indefinitely while looking merely stale.

    Only counts diagnostics from the last 7 days: an old one is history, not a
    live condition, and `routine_audit.py health` shows the full record.
    """
    failures_root = _meta_dir(ov) / "routine_failures"
    if not failures_root.is_dir():
        return None, "routine_failures/ absent; skip"

    cutoff = today - timedelta(days=7)
    recent: list[tuple[str, str, str]] = []
    debug_parts: list[str] = []
    for routine_dir in sorted(failures_root.iterdir()):
        if not routine_dir.is_dir():
            continue
        newest: tuple[str, str] | None = None
        for path in routine_dir.glob("*.toml"):
            try:
                record = tomllib.loads(path.read_text())
            except (tomllib.TOMLDecodeError, OSError):
                continue
            recorded = str(record.get("recorded_at") or "")[:10]
            if not recorded:
                continue
            if newest is None or recorded > newest[0]:
                newest = (recorded, str(record.get("phase") or "unknown"))
        if newest is None:
            continue
        try:
            when = date.fromisoformat(newest[0])
        except ValueError:
            continue
        if when < cutoff:
            debug_parts.append(f"{routine_dir.name}: {newest[0]} (older than 7d)")
            continue
        recent.append((routine_dir.name, newest[0], newest[1]))

    debug = "; ".join(debug_parts)
    if not recent:
        return None, debug or "no recent pre-claim failures"

    recent.sort(key=lambda item: item[1], reverse=True)
    listing = "; ".join(f"{name} ({phase} on {when})" for name, when, phase in recent[:3])
    if len(recent) > 3:
        listing += f", +{len(recent) - 3} more"

    return (
        Cue(
            key="routine_failures",
            severity="soft",
            command_path="scripts/launchd/README.md",
            message=(
                f"{len(recent)} routine(s) failed before writing a claim: {listing}. "
                "这类失败只留在 routine_failures/ 里, 其他 cue 看不到. "
                "`uv run scripts/routine_audit.py health` 看全表."
            ),
        ),
        debug,
    )


CHECKS = [
    ("weekly", check_weekly),
    ("intent_misses", check_intent_misses),
    ("eval_regression", check_eval_regression),
    ("meta_reflection", check_meta_reflection_due),
    ("zettelm", check_zettelm),
    ("recurring", check_recurring),
    ("aggregate_freshness", check_aggregate_freshness),
    ("routine_outputs", check_routine_outputs),
    ("routine_staleness", check_routine_staleness),
    ("routine_hitrate", check_routine_hitrate),
    ("routine_policy", check_routine_policy),
    ("autoevo_pending", check_autoevo_pending),
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
    parser.add_argument(
        "--touch-lock",
        action="store_true",
        help="Refresh the session-active lock and exit. No cue checks run; "
        "the lock path is resolved via the registry. Used by the "
        "UserPromptSubmit hook so long-running sessions keep the lock fresh "
        "without paying for a full sweep on every prompt.",
    )
    # Snooze subcommand: `cues.py snooze <key> [--days N]` writes a
    # per-key snooze entry to $OV/_meta/cue_snooze.json. The next session
    # skips fired cues whose key matches until the snooze expires.
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

    # --touch-lock: lightweight per-prompt refresh path used by the
    # UserPromptSubmit hook. Touches the lock and exits without running
    # any cue check. The lock path is resolved via the registry so a
    # rename of the `cache` segment in harness/paths.toml propagates
    # to this hook automatically.
    #
    # Critical: the scheduled headless runtime invocation is itself a
    # UserPromptSubmit event. Without the env-var guard below,
    # the hook would touch the lock right before /autoevo-nightly's
    # pre-flight gate checks the lock, causing the bot to abort every
    # night with "session-active lock fresh." The launchd runner exports
    # ATELIER_SKIP_LOCK_TOUCH=1 so the scheduled run bypasses the refresh.
    if args.touch_lock:
        _touch_session_lock(args.verbose, "UserPromptSubmit")
        return 0

    snoozes = _load_snoozes(ov)

    # Session-active lock: when invoked as a SessionStart hook, touch a
    # marker file so the 5am `/autoevo-nightly` bot can detect a recent
    # session and bail out per `protocols/autoevo.md` § Pre-flight gates.
    # SessionStart catches the start of a fresh session; the dedicated
    # `--touch-lock` path (above) handles the UserPromptSubmit per-prompt
    # refresh so long-running sessions stay protected past the 6h bail window.
    #
    # Skip-flag honor: same logic as --touch-lock. The launchd-invoked
    # headless autoevo runtime triggers SessionStart as well as
    # UserPromptSubmit; without this guard, the bot would touch the lock
    # right before its own pre-flight gate reads it, aborting every run.
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
        # A check that crashes is itself a finding: it means the one surface
        # that would report last night's failure may be blind. Log durably
        # and say so, instead of degrading silently to "no cue".
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
        print(json.dumps([asdict(c) for c in fired], ensure_ascii=False))
    else:
        for c in fired:
            print(f"{c.key}\t{c.severity}\t{c.command_path}\t{c.message}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
