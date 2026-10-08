#!/usr/bin/env python3
"""Atomic queue CLI for `$OV/_meta/autoevo_pending.toml`; one JSON result per call.

Use `<command> --help` for flags. `append --entries FILE|-` preserves existing
entries and dedupes sorted peers against pending or recently resolved entries
(--dedupe-days, default 90). `resolve` requires a reason, records the decision,
and anchors dedupe at resolved_at. `auto-dismiss` handles surface_count >= 3
or age beyond --max-age-days (default 30); `list` optionally filters status.

Defaults require `append --rule-defaults` or `set-default`; the latter starts
a fresh veto window. `defer` bumps surface_count/last_surfaced and the deadline;
`veto-expired` exposes due defaults to the nightly. A contradictory decision
vetoes: skip vetoes stale-banner, apply vetoes dismiss; skip confirms dismiss.
Schema and lifecycle: protocols/autoevo.md § Pending queue.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write, parse_iso_date, tier_segments, vault_root  # noqa: E402
import decisions  # noqa: E402  (the human-decision ledger; resolve/defer write one line each)

REQUIRED = ("id", "category", "proposed_action", "evidence_summary", "proposed_at", "status")
CATEGORIES = {"redundant", "time-stale-A", "time-stale-B", "contradicted", "low-signal"}
STATUSES = {"pending", "applied", "dismissed", "auto-dismissed"}
DEDUPE_STATUSES = STATUSES  # dedupe protects every resolved state, deliberately the same set
DEFAULT_VETO_DAYS = 14
DEFAULT_ACTIONS = {"time-stale-A": "stale-banner"}
DEFAULT_ELIGIBLE_TIERS = ("wip", "research")
SETTABLE_DEFAULTS = ("stale-banner", "dismiss")
# Categories a precedent default may touch at all, for EITHER action. The
# never-inferred list in protocols/decision-ledger.md § What is never inferred
# carves out `contradicted` (a wiki rewrite needs human approval) and
# `time-stale-B` (era judgments are intent-laden; protocols/autoevo.md § Log to
# pending queue says never auto-act). An allowlist rather than a denylist so a
# category added later stays human until someone decides otherwise.
PRECEDENT_ELIGIBLE_CATEGORIES = ("redundant", "time-stale-A", "low-signal")
LEDGER_VERDICT = {"applied": "apply", "dismissed": "dismiss"}


def queue_path() -> Path:
    return vault_root() / tier_segments().get("meta", "_meta") / "autoevo_pending.toml"


class QueueError(RuntimeError):
    """The queue file exists but cannot be parsed; refuse to touch it."""


def load(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": 1, "pending": []}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise QueueError(f"queue file unreadable, refusing to write: {exc}") from exc
    data.setdefault("schema_version", 1)
    data.setdefault("pending", [])
    return data


def _toml_value(value: object) -> object:
    if isinstance(value, list):
        return [_toml_value(item) for item in value]
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


ENTRY_KEY_ORDER = (
    "id", "category", "proposed_action", "evidence_summary", "proposed_at",
    "last_surfaced", "surface_count", "status", "default_action", "default_at",
    "peers", "dismiss_reason", "resolved_at",
)


def _table_body(entry: dict) -> dict:
    ordered = [k for k in ENTRY_KEY_ORDER if k in entry] + [k for k in entry if k not in ENTRY_KEY_ORDER]
    return {
        key: _toml_value(entry[key])
        for key in ordered
        if entry[key] is not None
        and not isinstance(entry[key], dict)
        and not (key == "peers" and not entry[key])
    }


def render(data: dict) -> str:
    """Render the queue while preserving unknown top-level TOML state."""
    import tomli_w

    document = dict(data)
    document["schema_version"] = int(data.get("schema_version", 1))
    document["pending"] = [_table_body(entry) for entry in data.get("pending", [])]
    text = tomli_w.dumps(document)
    tomllib.loads(text)
    return text


def _norm_peers(peers: object) -> tuple[str, ...]:
    if not isinstance(peers, list):
        return ()
    return tuple(sorted(str(p).strip().rstrip("/") for p in peers if str(p).strip()))


def _parse_date(value: object) -> date | None:
    return parse_iso_date(value)


def default_eligible_prefixes() -> tuple[str, ...]:
    segments = tier_segments()
    return tuple(f"{segments.get(key, key)}/" for key in DEFAULT_ELIGIBLE_TIERS)


def default_for(entry: dict) -> str | None:
    """The default action an unvetoed entry receives, or None (stays human)."""
    action = DEFAULT_ACTIONS.get(str(entry.get("category")))
    if not action:
        return None
    peers = _norm_peers(entry.get("peers"))
    if not peers:
        return None
    prefixes = default_eligible_prefixes()
    if not all(peer.startswith(prefixes) for peer in peers):
        return None
    return action


def validate_entry(entry: dict) -> list[str]:
    problems = []
    for key in REQUIRED:
        if not entry.get(key):
            problems.append(f"missing {key}")
    if entry.get("category") not in CATEGORIES:
        problems.append(f"unknown category {entry.get('category')!r}")
    if entry.get("status") not in STATUSES:
        problems.append(f"unknown status {entry.get('status')!r}")
    if _parse_date(entry.get("proposed_at")) is None:
        problems.append("proposed_at is not YYYY-MM-DD")
    return problems


def cmd_append(args: argparse.Namespace) -> int:
    try:
        raw = sys.stdin.read() if args.entries == "-" else Path(args.entries).read_text(encoding="utf-8")
        new_entries = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        # Keep the tool's contract (one JSON object, always) even when the
        # model-written input is malformed; the nightly command routes this
        # to audit § Errors instead of crashing at the queue-write step.
        print(json.dumps({"error": f"cannot read entries: {exc}"[:300], "appended": [], "skipped": [], "invalid": []}))
        return 2
    if not isinstance(new_entries, list):
        print(json.dumps({"error": "entries must be a JSON list", "appended": [], "skipped": [], "invalid": []}))
        return 2
    path = queue_path() if args.queue is None else Path(args.queue)
    try:
        data = load(path)
    except QueueError as exc:
        # Nightly contract: never overwrite a corrupted queue; park the
        # proposed entries in a sidecar for manual recovery and report both.
        # Never clobber an earlier parked sidecar: hourly retries against the
        # same corrupted queue each park their own proposals.
        sidecar = path.with_name(path.name + ".new")
        counter = 0
        while sidecar.exists():
            counter += 1
            sidecar = path.with_name(f"{path.name}.new-{counter}")
        valid = [e for e in new_entries if isinstance(e, dict) and not validate_entry(e)]
        if valid:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(render({"schema_version": 1, "pending": valid}), encoding="utf-8")
        print(
            json.dumps(
                {
                    "error": str(exc)[:300],
                    "appended": [],
                    "skipped": [],
                    "invalid": [],
                    "sidecar": str(sidecar) if valid else None,
                },
                sort_keys=True,
            )
        )
        return 2
    existing = data["pending"]
    today = _parse_date(args.today) or date.today()
    horizon = today - timedelta(days=args.dedupe_days)

    seen: dict[tuple[str, ...], dict] = {}
    for entry in existing:
        peers = _norm_peers(entry.get("peers"))
        if not peers or entry.get("status") not in DEDUPE_STATUSES:
            continue
        # Protect the cluster for `dedupe_days` after the user's DECISION, not
        # after the proposal: a dismissal on day 30 would otherwise lapse on
        # day 90 from proposal.
        anchor = _parse_date(
            entry.get("resolved_at") or entry.get("last_surfaced") or entry.get("proposed_at")
        )
        if entry.get("status") == "pending" or (anchor and anchor >= horizon):
            seen.setdefault(peers, entry)
    existing_ids = {e.get("id") for e in existing}

    appended, skipped, invalid = [], [], []
    for entry in new_entries:
        if not isinstance(entry, dict):
            invalid.append({"id": None, "problems": [f"entry is {type(entry).__name__}, not an object"]})
            continue
        problems = validate_entry(entry)
        if problems:
            invalid.append({"id": entry.get("id"), "problems": problems})
            continue
        if entry["id"] in existing_ids:
            skipped.append({"id": entry["id"], "reason": "duplicate id"})
            continue
        peers = _norm_peers(entry.get("peers"))
        if peers and peers in seen:
            prior = seen[peers]
            skipped.append(
                {
                    "id": entry["id"],
                    "reason": f"same peers as {prior.get('id')} ({prior.get('status')})",
                }
            )
            continue
        entry.setdefault("last_surfaced", entry["proposed_at"])
        entry.setdefault("surface_count", 0)
        default_action = default_for(entry) if args.rule_defaults else None
        if default_action:
            proposed = _parse_date(entry["proposed_at"]) or today
            entry["default_action"] = default_action
            entry["default_at"] = (proposed + timedelta(days=args.veto_days)).isoformat()
        existing.append(entry)
        existing_ids.add(entry["id"])
        if peers:
            seen[peers] = entry
        appended.append(entry["id"])

    if appended:
        atomic_write(path, render(data))
    print(
        json.dumps(
            {
                "queue": str(path),
                "appended": appended,
                "skipped": skipped,
                "invalid": invalid,
                "total": len(existing),
            },
            sort_keys=True,
        )
    )
    # Invalid entries are reported in the JSON for audit § Errors; the run
    # itself completed, so exit 0 (a nonzero exit under `set -e` would abort
    # the nightly after valid entries were already appended).
    return 0


def cmd_auto_dismiss(args: argparse.Namespace) -> int:
    path = queue_path() if args.queue is None else Path(args.queue)
    data = load(path)
    today = _parse_date(args.today) or date.today()
    cutoff = today - timedelta(days=args.max_age_days)
    dismissed = []
    for entry in data["pending"]:
        if entry.get("status") != "pending":
            continue
        proposed = _parse_date(entry.get("proposed_at"))
        by_count = int(entry.get("surface_count", 0)) >= 3
        by_age = proposed is not None and proposed < cutoff
        if by_count or by_age:
            reason = "surface_count>=3" if by_count else f"older than {args.max_age_days}d"
            dismissed.append({"id": entry.get("id"), "category": entry.get("category"), "reason": reason})
            if args.dry_run:
                continue
            entry["status"] = "auto-dismissed"
            entry["dismiss_reason"] = reason
            entry["resolved_at"] = today.isoformat()
    if dismissed and not args.dry_run:
        atomic_write(path, render(data))
    payload = {"queue": str(path), "auto_dismissed": dismissed}
    if args.dry_run:
        payload["dry_run"] = True
    print(json.dumps(payload, sort_keys=True))
    return 0


def resolve_entry(
    path: Path, entry_id: str, status: str, reason: str, *, today: date,
    source: str = "autoevo-review", by: str = "human", ledger: Path | None = None,
    explicit_today: bool = False,
) -> dict:
    """Mark one entry applied or dismissed and write the decision ledger line."""
    data = load(path)
    for entry in data["pending"]:
        if entry.get("id") != entry_id:
            continue
        if entry.get("status") != "pending":
            return {"error": f"{entry_id} is {entry.get('status')}, not pending"}
        entry["status"] = status
        entry["resolved_at"] = today.isoformat()
        entry["last_surfaced"] = today.isoformat()
        entry["dismiss_reason"] = reason
        atomic_write(path, render(data))
        line = decisions.record_best_effort(
            cls=f"autoevo/{entry.get('category')}",
            subject=str(entry.get("id")),
            verdict=LEDGER_VERDICT[status],
            reason=reason,
            features=decisions.autoevo_features(entry),
            source=source,
            by=by,
            ts=f"{today.isoformat()}T00:00:00" if explicit_today else None,
            path=ledger,
        )
        return {"queue": str(path), "resolved": entry_id, "status": status, "ledger": "recorded" if line else "skipped"}
    return {"error": f"no entry with id {entry_id}"}


def cmd_resolve(args: argparse.Namespace) -> int:
    """Mark one entry applied or dismissed (the /autoevo-review write path)."""
    path = queue_path() if args.queue is None else Path(args.queue)
    today = _parse_date(args.today) or date.today()
    result = resolve_entry(
        path, args.id, args.status, args.reason, today=today, source=args.source, by=args.by,
        ledger=Path(args.ledger) if args.ledger else None, explicit_today=bool(args.today),
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if "error" not in result else 1


def cmd_defer(args: argparse.Namespace) -> int:
    """Increment surface_count and bump last_surfaced for one pending entry."""
    path = queue_path() if args.queue is None else Path(args.queue)
    data = load(path)
    today = _parse_date(args.today) or date.today()
    for entry in data["pending"]:
        if entry.get("id") != args.id:
            continue
        if entry.get("status") != "pending":
            print(json.dumps({"error": f"{args.id} is {entry.get('status')}, not pending"}))
            return 1
        entry["surface_count"] = int(entry.get("surface_count", 0)) + 1
        entry["last_surfaced"] = today.isoformat()
        payload = {"queue": str(path), "deferred": args.id, "surface_count": entry["surface_count"]}
        if entry.get("default_at"):
            # The user looked and asked for more time: a fresh veto window.
            entry["default_at"] = (today + timedelta(days=DEFAULT_VETO_DAYS)).isoformat()
            payload["default_at"] = entry["default_at"]
        atomic_write(path, render(data))
        if args.reason:
            line = decisions.record_best_effort(
                cls=f"autoevo/{entry.get('category')}", subject=str(entry.get("id")), verdict="defer",
                reason=args.reason, features=decisions.autoevo_features(entry), source="autoevo-review",
                by="human", path=Path(args.ledger) if args.ledger else None,
            )
            payload["ledger"] = "recorded" if line else "skipped"
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(json.dumps({"error": f"no entry with id {args.id}"}))
    return 1


def cmd_set_default(args: argparse.Namespace) -> int:
    """Give one pending entry a default with a fresh veto window."""
    path = queue_path() if args.queue is None else Path(args.queue)
    data = load(path)
    today = _parse_date(args.today) or date.today()
    for entry in data["pending"]:
        if entry.get("id") != args.id:
            continue
        if entry.get("status") != "pending":
            print(json.dumps({"error": f"{args.id} is {entry.get('status')}, not pending"}))
            return 1
        category = str(entry.get("category"))
        if category not in PRECEDENT_ELIGIBLE_CATEGORIES:
            print(json.dumps({"error": f"{args.id} category {category!r} is never inferred; it stays a human decision"}))
            return 1
        if args.action == "stale-banner" and default_for(entry) != "stale-banner":
            print(json.dumps({"error": f"{args.id} is not eligible for stale-banner (category or tier)"}))
            return 1
        entry["default_action"] = args.action
        entry["default_at"] = (today + timedelta(days=args.veto_days)).isoformat()
        atomic_write(path, render(data))
        payload = {"queue": str(path), "id": args.id, "default_action": args.action, "default_at": entry["default_at"]}
        if args.reason:
            line = decisions.record_best_effort(
                cls=f"autoevo/{entry.get('category')}", subject=str(entry.get("id")),
                verdict="apply" if args.action == "stale-banner" else "dismiss", reason=args.reason,
                features=decisions.autoevo_features(entry), source=args.source, by=args.by,
                ts=f"{today.isoformat()}T00:00:00" if args.today else None,
                path=Path(args.ledger) if args.ledger else None,
            )
            payload["ledger"] = "recorded" if line else "skipped"
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(json.dumps({"error": f"no entry with id {args.id}"}))
    return 1


def cmd_veto_expired(args: argparse.Namespace) -> int:
    """Pending entries whose veto window has closed.

    Read-only unless --apply-dismissals: then `dismiss` defaults are resolved
    here (no file op is involved) and only entries needing a git op are
    returned under `expired`.
    """
    path = queue_path() if args.queue is None else Path(args.queue)
    data = load(path)
    today = _parse_date(args.today) or date.today()
    expired, dismissed, resolved = [], [], []
    for entry in data["pending"]:
        if entry.get("status") != "pending" or not entry.get("default_action"):
            continue
        deadline = _parse_date(entry.get("default_at"))
        if deadline is None or deadline > today:
            continue
        if entry.get("default_action") == "dismiss" and args.apply_dismissals:
            entry["status"] = "dismissed"
            entry["resolved_at"] = today.isoformat()
            entry["last_surfaced"] = today.isoformat()
            entry["dismiss_reason"] = "default after veto window"
            dismissed.append(entry.get("id"))
            resolved.append(entry)
            continue
        expired.append(
            {
                key: entry.get(key)
                for key in (
                    "id", "category", "default_action", "default_at", "proposed_at",
                    "proposed_action", "evidence_summary", "peers",
                )
            }
        )
    if resolved:
        atomic_write(path, render(data))
        for entry in resolved:
            decisions.record_best_effort(
                cls=f"autoevo/{entry.get('category')}",
                subject=str(entry.get("id")),
                verdict=LEDGER_VERDICT["dismissed"],
                reason="default after veto window",
                features=decisions.autoevo_features(entry),
                source="nightly",
                by="rule",
                ts=f"{today.isoformat()}T00:00:00" if args.today else None,
                path=Path(args.ledger) if args.ledger else None,
            )
    print(json.dumps({"queue": str(path), "today": today.isoformat(), "expired": expired, "dismissed": dismissed}, sort_keys=True, default=str))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    path = queue_path() if args.queue is None else Path(args.queue)
    data = load(path)
    entries = data["pending"]
    if args.status:
        entries = [e for e in entries if e.get("status") == args.status]
    print(json.dumps({"queue": str(path), "count": len(entries), "entries": entries}, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queue", help="Override queue path (tests).")
    parser.add_argument("--ledger", help="Override the decision ledger path (tests).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append")
    p_append.add_argument("--entries", required=True, help="JSON file with a list of entries, or - for stdin")
    p_append.add_argument("--dedupe-days", type=int, default=90)
    p_append.add_argument("--today", default=None)
    p_append.add_argument("--veto-days", type=int, default=DEFAULT_VETO_DAYS)
    p_append.add_argument("--rule-defaults", action="store_true", help="Stamp the fixed DEFAULT_ACTIONS rule instead of waiting for the precedent judge.")
    p_append.set_defaults(func=cmd_append)

    p_ad = sub.add_parser("auto-dismiss")
    p_ad.add_argument("--max-age-days", type=int, default=30)
    p_ad.add_argument("--today", default=None)
    p_ad.add_argument(
        "--dry-run",
        action="store_true",
        help="List the entries the housekeeping would dismiss without writing the queue.",
    )
    p_ad.set_defaults(func=cmd_auto_dismiss)

    p_res = sub.add_parser("resolve")
    p_res.add_argument("--id", required=True)
    p_res.add_argument("--status", required=True, choices=["applied", "dismissed"])
    p_res.add_argument("--reason", required=True, help="One sentence; becomes a precedent in the decision ledger.")
    p_res.add_argument("--today", default=None)
    p_res.add_argument("--source", default="autoevo-review")
    p_res.add_argument("--by", default="human", choices=decisions.BY_VALUES)
    p_res.set_defaults(func=cmd_resolve)

    p_def = sub.add_parser("defer")
    p_def.add_argument("--id", required=True)
    p_def.add_argument("--today", default=None)
    p_def.add_argument("--reason", default=None, help="Optional; recorded as a defer precedent when given.")
    p_def.set_defaults(func=cmd_defer)

    p_set = sub.add_parser("set-default")
    p_set.add_argument("--id", required=True)
    p_set.add_argument("--action", required=True, choices=SETTABLE_DEFAULTS)
    p_set.add_argument("--today", default=None)
    p_set.add_argument("--veto-days", type=int, default=DEFAULT_VETO_DAYS)
    p_set.add_argument("--reason", default=None, help="Recorded in the ledger with --by (precedent | rule).")
    p_set.add_argument("--by", default="precedent", choices=decisions.BY_VALUES)
    p_set.add_argument("--source", default="nightly")
    p_set.set_defaults(func=cmd_set_default)

    p_veto = sub.add_parser("veto-expired")
    p_veto.add_argument("--today", default=None)
    p_veto.add_argument("--apply-dismissals", action="store_true", help="Resolve expired `dismiss` defaults in place.")
    p_veto.set_defaults(func=cmd_veto_expired)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except QueueError as exc:
        print(json.dumps({"error": str(exc)[:300], "appended": [], "skipped": [], "invalid": []}))
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # keep the one-JSON-object contract for headless callers
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
