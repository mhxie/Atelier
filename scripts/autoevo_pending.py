#!/usr/bin/env python3
"""Deterministic reader/writer for `$OV/_meta/autoevo_pending.toml`.

Why this exists: the nightly command hand-emitted TOML from the model and
appended findings without checking what the user had already dismissed, so
the same clusters were re-proposed after every 30-day auto-dismiss. Queue
writes are mechanical: escaping, dedupe, atomic replace, resolution, and
auto-dismiss belong in a script.

Subcommands (all print one JSON object):

  append --entries FILE|-   Append new findings (JSON list of entry dicts).
                            Skips any finding whose sorted `peers` equal an
                            existing entry's peers when that entry is
                            pending, applied, or dismissed within
                            --dedupe-days (default 90). Never rewrites
                            existing entries.
  auto-dismiss --today D    Mark pending entries with surface_count >= 3 or
                            proposed_at older than --max-age-days (default
                            30) as auto-dismissed.
  resolve --id X --status applied|dismissed [--reason R]
                            Record a /autoevo-review decision (sets
                            resolved_at, which anchors the dedupe window).
  defer --id X              Increment surface_count, bump last_surfaced.
  list [--status S]         Entries, optionally filtered.

Entry fields follow protocols/autoevo.md § Pending queue.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write as _atomic_write, tier_segments, vault_root  # noqa: E402

REQUIRED = ("id", "category", "proposed_action", "evidence_summary", "proposed_at", "status")
CATEGORIES = {"redundant", "time-stale-A", "time-stale-B", "contradicted", "low-signal"}
STATUSES = {"pending", "applied", "dismissed", "auto-dismissed"}
DEDUPE_STATUSES = STATUSES  # dedupe protects every resolved state, deliberately the same set


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


def _esc(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return f'"{_esc(value)}"'


ENTRY_KEY_ORDER = (
    "id", "category", "proposed_action", "evidence_summary", "proposed_at",
    "last_surfaced", "surface_count", "status", "peers", "dismiss_reason", "resolved_at",
)


def _render_table_body(entry: dict, key_order: tuple[str, ...] = ENTRY_KEY_ORDER) -> list[str]:
    lines: list[str] = []
    ordered = [k for k in key_order if k in entry] + [k for k in entry if k not in key_order]
    for key in ordered:
        value = entry[key]
        if value is None or isinstance(value, dict):
            continue  # nested tables are not part of this schema
        if key == "peers" and not value:
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    return lines


def render_entry(entry: dict, table: str = "pending") -> str:
    return "\n".join([f"[[{table}]]", *_render_table_body(entry)]) + "\n"


def render(data: dict) -> str:
    """Render the whole queue file. Unknown top-level tables are preserved.

    The live file once gained an 18-entry `[[finding]]` array because a
    model-emitted write used the wrong table name; the helper keeps such
    data verbatim rather than silently dropping it.
    """
    out = [f"schema_version = {int(data.get('schema_version', 1))}", ""]
    for key, value in data.items():
        if key in {"schema_version", "pending"}:
            continue
        if isinstance(value, dict):
            out.append(f"[{key}]")
            out.extend(_render_table_body(value, ()))
            out.append("")
        elif isinstance(value, list) and all(isinstance(v, dict) for v in value):
            for item in value:
                out.append(render_entry(item, key))
        else:
            out.append(f"{key} = {_toml_value(value)}")
            out.append("")
    for entry in data.get("pending", []):
        out.append(render_entry(entry))
    return "\n".join(out)


def atomic_write(path: Path, text: str) -> None:
    _atomic_write(path, text)


def _norm_peers(peers: object) -> tuple[str, ...]:
    if not isinstance(peers, list):
        return ()
    return tuple(sorted(str(p).strip().rstrip("/") for p in peers if str(p).strip()))


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


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
            sidecar.write_text("".join(render_entry(e) + "\n" for e in valid), encoding="utf-8")
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


def cmd_resolve(args: argparse.Namespace) -> int:
    """Mark one entry applied or dismissed (the /autoevo-review write path)."""
    path = queue_path() if args.queue is None else Path(args.queue)
    data = load(path)
    today = _parse_date(args.today) or date.today()
    for entry in data["pending"]:
        if entry.get("id") != args.id:
            continue
        if entry.get("status") != "pending":
            print(json.dumps({"error": f"{args.id} is {entry.get('status')}, not pending"}))
            return 1
        entry["status"] = args.status
        entry["resolved_at"] = today.isoformat()
        entry["last_surfaced"] = today.isoformat()
        if args.reason:
            entry["dismiss_reason"] = args.reason
        atomic_write(path, render(data))
        print(json.dumps({"queue": str(path), "resolved": args.id, "status": args.status}, sort_keys=True))
        return 0
    print(json.dumps({"error": f"no entry with id {args.id}"}))
    return 1


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
        atomic_write(path, render(data))
        print(json.dumps({"queue": str(path), "deferred": args.id, "surface_count": entry["surface_count"]}, sort_keys=True))
        return 0
    print(json.dumps({"error": f"no entry with id {args.id}"}))
    return 1


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
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append")
    p_append.add_argument("--entries", required=True, help="JSON file with a list of entries, or - for stdin")
    p_append.add_argument("--dedupe-days", type=int, default=90)
    p_append.add_argument("--today", default=None)
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
    p_res.add_argument("--reason", default=None)
    p_res.add_argument("--today", default=None)
    p_res.set_defaults(func=cmd_resolve)

    p_def = sub.add_parser("defer")
    p_def.add_argument("--id", required=True)
    p_def.add_argument("--today", default=None)
    p_def.set_defaults(func=cmd_defer)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except QueueError as exc:
        print(json.dumps({"error": str(exc)[:300], "appended": [], "skipped": [], "invalid": []}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
