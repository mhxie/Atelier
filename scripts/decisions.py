#!/usr/bin/env python3
"""Unified human-decision ledger: `$OV/_meta/decisions.jsonl`.

Every verdict a person gives the system (apply, dismiss, defer, undo, a
routing clarification, a triage call) is one JSON line here, with the one
sentence of reason that makes it a precedent instead of a click. Readers:
`scripts/precedent.py` (nearest past decisions for a new item, and the
per-class accuracy gate) and `/system-review`.

Line shape:
  {"ts": "2026-09-02T10:00:00", "class": "autoevo/time-stale-A",
   "subject": "<queue id, path, or phrase>", "verdict": "apply|dismiss|defer|undo|clarified:<x>|...",
   "reason": "<one sentence>", "features": {...}, "source": "autoevo-review|hi|triage|nightly",
   "by": "human|precedent|rule"}

`by` separates a person's verdict from a default the system chose; only
human lines are precedents, and a later human line that contradicts a
precedent line on the same subject is a veto. Silence (auto-dismiss) is
never recorded: it is not a decision.

Subcommands (each prints one JSON object):
  record          append one line (reason required)
  import-autoevo  backfill resolved queue entries (applied / dismissed; never auto-dismissed)
  list            lines, optionally by --class / --since / --subject
  stats           per-class verdict counts and precedent accuracy
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier_segments  # noqa: E402

LEDGER_FALLBACK = Path.home() / ".cache" / "atelier" / "decisions.jsonl"
BY_VALUES = ("human", "precedent", "rule")
MIN_REASON_CHARS = 3
VETO_WINDOW_DAYS = 14


def ledger_path() -> Path:
    ov = os.environ.get("OV")
    if ov:
        return Path(ov) / tier_segments().get("meta", "_meta") / "decisions.jsonl"
    return LEDGER_FALLBACK


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record(
    *,
    cls: str,
    subject: str,
    verdict: str,
    reason: str,
    features: dict[str, Any] | None = None,
    source: str = "",
    by: str = "human",
    ts: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one decision line. Raises ValueError on a missing reason."""
    reason = " ".join(str(reason).split())
    if len(reason) < MIN_REASON_CHARS:
        raise ValueError("a decision needs a reason (one sentence)")
    if by not in BY_VALUES:
        raise ValueError(f"by must be one of {BY_VALUES}")
    line = {
        "ts": ts or _now(),
        "class": str(cls).strip(),
        "subject": str(subject).strip(),
        "verdict": str(verdict).strip(),
        "reason": reason,
        "features": features or {},
        "source": source,
        "by": by,
    }
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # One encoded blob, one write(2), under an exclusive lock. A ledger line
    # carries `features` (peers, evidence summary) and routinely exceeds the
    # 4096-byte limit below which O_APPEND alone is atomic, and the nightly's
    # set-default subprocess can run while an interactive resolve writes. A torn
    # line is invisible in both directions: `load` drops unparseable lines
    # silently, so corruption shows up as a precedent that quietly stopped
    # existing. The repo already locks this class of write (scripts/routine_lock).
    blob = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # os.write may write fewer bytes than asked even for a regular file;
            # holding the lock does not make a short write whole, so drain it.
            view = memoryview(blob)
            while view:
                view = view[os.write(fd, view):]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return line


def record_best_effort(**kwargs: Any) -> dict[str, Any] | None:
    """Ledger writes never block a live command; failures go to stderr."""
    try:
        return record(**kwargs)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"atelier: decision ledger skipped ({exc})\n")
        return None


def load(path: Path | None = None, *, since: date | None = None) -> list[dict[str, Any]]:
    target = path or ledger_path()
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            # Never silently shrink the ledger: a dropped line changes precedent
            # counts and the accuracy denominator with no diagnostic anywhere.
            sys.stderr.write(f"atelier: decision ledger skipped an unparseable line in {target}\n")
            continue
        if not isinstance(row, dict):
            continue
        if since is not None:
            try:
                if date.fromisoformat(str(row.get("ts", ""))[:10]) < since:
                    continue
            except ValueError:
                continue
        rows.append(row)
    return rows


def _parse_features(pairs: list[str] | None, blob: str | None) -> dict[str, Any]:
    features: dict[str, Any] = {}
    if blob:
        loaded = json.loads(blob)
        if not isinstance(loaded, dict):
            raise ValueError("--features-json must be an object")
        features.update(loaded)
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--feature expects k=v, got {pair!r}")
        features[key.strip()] = value
    return features


def cmd_record(args: argparse.Namespace) -> int:
    try:
        line = record(
            cls=args.cls,
            subject=args.subject,
            verdict=args.verdict,
            reason=args.reason,
            features=_parse_features(args.feature, args.features_json),
            source=args.source,
            by=args.by,
            path=Path(args.ledger) if args.ledger else None,
        )
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps({"ledger": str(Path(args.ledger) if args.ledger else ledger_path()), "recorded": line}, ensure_ascii=False))
    return 0


def _tier_of(paths: list[str]) -> str:
    firsts = {str(p).strip("/").split("/", 1)[0] for p in paths if str(p).strip()}
    return firsts.pop() if len(firsts) == 1 else ",".join(sorted(firsts))


def autoevo_features(entry: dict[str, Any]) -> dict[str, Any]:
    peers = [str(p) for p in (entry.get("peers") or []) if str(p).strip()]
    return {
        "category": entry.get("category"),
        "peers": peers,
        "tier": _tier_of(peers),
        "proposed_action": entry.get("proposed_action"),
        "evidence_summary": entry.get("evidence_summary"),
        "proposed_at": entry.get("proposed_at"),
        "surface_count": entry.get("surface_count", 0),
    }


def cmd_import_autoevo(args: argparse.Namespace) -> int:
    """Backfill: resolved queue entries become ledger lines, once."""
    queue = Path(args.queue) if args.queue else (
        ledger_path().parent / "autoevo_pending.toml"
    )
    if not queue.is_file():
        print(json.dumps({"error": f"queue missing: {queue}", "imported": []}))
        return 1
    try:
        entries = tomllib.loads(queue.read_text(encoding="utf-8")).get("pending", [])
    except tomllib.TOMLDecodeError as exc:
        print(json.dumps({"error": f"queue unreadable: {exc}", "imported": []}))
        return 1
    target = Path(args.ledger) if args.ledger else ledger_path()
    existing = {(r.get("class"), r.get("subject"), r.get("verdict")) for r in load(target)}
    imported, skipped = [], []
    for entry in entries:
        status = entry.get("status")
        if status not in {"applied", "dismissed"}:
            skipped.append({"id": entry.get("id"), "reason": f"status {status}"})
            continue
        cls = f"autoevo/{entry.get('category')}"
        verdict = "apply" if status == "applied" else "dismiss"
        key = (cls, entry.get("id"), verdict)
        if key in existing:
            skipped.append({"id": entry.get("id"), "reason": "already in ledger"})
            continue
        reason = entry.get("dismiss_reason") or "(no reason recorded at the time)"
        by = "rule" if str(reason).startswith("default after veto window") else "human"
        ts = str(entry.get("resolved_at") or entry.get("last_surfaced") or entry.get("proposed_at") or date.today().isoformat())
        if args.dry_run:
            imported.append({"id": entry.get("id"), "verdict": verdict, "by": by})
            continue
        record(
            cls=cls, subject=str(entry.get("id")), verdict=verdict, reason=str(reason),
            features=autoevo_features(entry), source="autoevo-review", by=by,
            ts=f"{ts[:10]}T00:00:00", path=target,
        )
        existing.add(key)
        imported.append({"id": entry.get("id"), "verdict": verdict, "by": by})
    payload = {"ledger": str(target), "imported": imported, "skipped": skipped}
    if args.dry_run:
        payload["dry_run"] = True
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    since = date.fromisoformat(args.since) if args.since else None
    rows = load(Path(args.ledger) if args.ledger else None, since=since)
    if args.cls:
        rows = [r for r in rows if r.get("class") == args.cls]
    if args.subject:
        rows = [r for r in rows if r.get("subject") == args.subject]
    print(json.dumps({"count": len(rows), "rows": rows}, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


def unconfirmed_since_heartbeat(rows: list[dict[str, Any]], cls: str) -> int:
    """Precedent defaults in `cls` written after the ledger's newest human line.

    The silent budget spends this. The heartbeat is any-class on purpose: a
    judge that works empties its own class of reviewable items, so a per-class
    heartbeat would starve and the budget would never reset. Any decision the
    user makes anywhere is evidence they are still watching, and it refills the
    budget for every class at once.

    This counts what `precedent_accuracy` cannot see. Accuracy treats a default
    that aged out unchallenged as correct, so it rises while nobody looks; this
    number rises for exactly the same reason and is the one that stops the judge.
    """
    heartbeat = max((str(r.get("ts", "")) for r in rows if r.get("by") == "human"), default="")
    return sum(
        1 for r in rows
        if r.get("by") == "precedent" and str(r.get("class")) == cls and str(r.get("ts", "")) > heartbeat
    )


def precedent_stats(rows: list[dict[str, Any]], today: date, cls: str | None = None) -> dict[str, dict[str, Any]]:
    """Per class: human verdict counts and how often precedent defaults stood.

    A precedent line is judged only when a later human line exists for the same
    subject; a later human verdict that differs is a veto. A line whose veto
    window has passed with no human line at all is `precedent_unconfirmed`, not
    judged: nobody looked, so it is evidence of nothing. `precedent_accuracy` is
    therefore over observed outcomes only, and is None until one exists.
    """
    by_class: dict[str, dict[str, Any]] = {}
    subjects: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        subjects.setdefault((str(row.get("class")), str(row.get("subject"))), []).append(row)
    for row in rows:
        row_cls = str(row.get("class"))
        if cls and row_cls != cls:
            continue
        stats = by_class.setdefault(
            row_cls,
            {"human": {}, "with_reason": 0, "human_total": 0, "precedent_total": 0,
             "precedent_judged": 0, "precedent_vetoed": 0, "precedent_unconfirmed": 0,
             "precedent_accuracy": None},
        )
        if row.get("by") == "human":
            stats["human_total"] += 1
            verdict = str(row.get("verdict"))
            stats["human"][verdict] = stats["human"].get(verdict, 0) + 1
            if str(row.get("reason", "")).strip() and not str(row.get("reason", "")).startswith("(no reason"):
                stats["with_reason"] += 1
        elif row.get("by") == "precedent":
            stats["precedent_total"] += 1
            later_human = [
                r for r in subjects.get((row_cls, str(row.get("subject"))), [])
                if r.get("by") == "human" and str(r.get("ts", "")) > str(row.get("ts", ""))
            ]
            if later_human:
                stats["precedent_judged"] += 1
                if any(r.get("verdict") != row.get("verdict") and r.get("verdict") != "defer" for r in later_human):
                    stats["precedent_vetoed"] += 1
            else:
                # Aging out is not a verdict. Counting it as judged made accuracy
                # rise for every default nobody looked at, so the measure of
                # whether the judge is right improved fastest when nobody was
                # checking. Unconfirmed defaults get their own count instead.
                try:
                    aged = date.fromisoformat(str(row.get("ts", ""))[:10]) <= today - timedelta(days=VETO_WINDOW_DAYS)
                except ValueError:
                    aged = False
                if aged:
                    stats["precedent_unconfirmed"] += 1
    for stats in by_class.values():
        if stats["precedent_judged"]:
            stats["precedent_accuracy"] = round(1 - stats["precedent_vetoed"] / stats["precedent_judged"], 3)
    return by_class


def cmd_stats(args: argparse.Namespace) -> int:
    rows = load(Path(args.ledger) if args.ledger else None)
    today = date.fromisoformat(args.today) if args.today else date.today()
    print(json.dumps({"ledger": str(Path(args.ledger) if args.ledger else ledger_path()), "classes": precedent_stats(rows, today, args.cls)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", default=None, help="Override the ledger path (tests).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="Append one decision line.")
    p.add_argument("--class", dest="cls", required=True, help="e.g. autoevo/time-stale-A, hi/route, triage/intent-coverage")
    p.add_argument("--subject", required=True)
    p.add_argument("--verdict", required=True)
    p.add_argument("--reason", required=True, help="One sentence; this is what makes the line a precedent.")
    p.add_argument("--source", default="")
    p.add_argument("--by", default="human", choices=BY_VALUES)
    p.add_argument("--feature", action="append", help="k=v (repeat)")
    p.add_argument("--features-json", default=None)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("import-autoevo", help="Backfill resolved queue entries into the ledger.")
    p.add_argument("--queue", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_import_autoevo)

    p = sub.add_parser("list")
    p.add_argument("--class", dest="cls", default=None)
    p.add_argument("--subject", default=None)
    p.add_argument("--since", default=None)
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("stats")
    p.add_argument("--class", dest="cls", default=None)
    p.add_argument("--today", default=None)
    p.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
