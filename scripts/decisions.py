#!/usr/bin/env python3
"""Decision and typed reading-evidence ledger: `$OV/_meta/decisions.jsonl`.

`record` appends a reasoned verdict; `list` filters it; `stats` measures verdicts
and precedent accuracy. `reading-*` owns typed policy/event/evaluation evidence.
All commands return JSON. Schema and consumers: protocols/decision-ledger.md.
Only `by=human` lines are precedents; a later contradictory human decision on
the same subject vetoes a precedent. Silence/auto-dismiss is not a decision
and is never recorded.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier_segments  # noqa: E402
import reading_feedback as reading  # noqa: E402

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


def _append_rows(fd: int, rows: list[dict]) -> None:
    blob = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    view = memoryview(blob)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("decision ledger write made no progress")
        view = view[written:]


def policy_record(policy: dict) -> dict:
    reading.validate_policy(policy)
    return {"ts": _now(), "class": reading.POLICY_CLASS, "subject": policy["id"],
            "verdict": "captured", "reason": "Exact selection policy snapshot",
            "features": {"schema": 1, "policy": policy}, "source": "reading", "by": "rule"}


def record_reading(rows: list[dict], path: Path | None = None) -> dict:
    """Validate and deduplicate a whole batch under the ledger's shared writer lock.

    Policy text is written once; new proposals retain only its identity. Legacy
    inline snapshots remain resolvable without rewriting any existing line.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("reading batch needs at least one event or policy")
    policies, events = [], []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("reading batch entries must be objects")
        if row.get("class") == reading.POLICY_CLASS:
            reading.validate_policy_record(row)
            policies.append(row)
        else:
            reading.validate(row, resolve_policy=False)
            if "policy" in row["features"]:
                policies.append(policy_record(row["features"]["policy"]))
            events.append({**row, "features": {k: v for k, v in row["features"].items() if k != "policy"}})
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = load(target, strict=True)
        stored, errors = reading.policy_index(existing)
        resolved, new_errors = reading.policy_index(existing + policies)
        if errors or new_errors:
            raise ValueError("repair invalid policy records before appending reading evidence")
        for row in events:
            reading.validate(row, resolved)
        pending, policy_duplicates = [], 0
        for row in policies:
            key = row["subject"]
            if key in stored:
                if stored[key] != row["features"]["policy"]:
                    raise ValueError("conflicting policy snapshot")
                policy_duplicates += 1
            else:
                stored[key] = row["features"]["policy"]
                pending.append(row)
        policy_count = len(pending)
        seen = {}
        for row in existing:
            if row.get("class") != reading.CLASS:
                continue
            f = row.get("features")
            if not isinstance(f, dict) or not isinstance(f.get("event_id"), str):
                continue
            key = f["event_id"]
            try:
                reading.validate(row, resolved)
                identity = reading.event_identity(row)
            except (ValueError, TypeError):
                identity = None
            # Conflicting historical IDs stay poisoned, not "fixed" by a retry.
            seen[key] = identity if key not in seen or seen[key] == identity else None
        duplicates = 0
        for row in events:
            key, identity = row["features"]["event_id"], reading.event_identity(row)
            if key in seen:
                if seen[key] != identity:
                    raise ValueError("conflicting event identity")
                duplicates += 1
            else:
                seen[key] = identity
                pending.append(row)
        _append_rows(fd, pending)
        if not events:
            return {"id": policies[0]["subject"], "recorded": policy_count, "duplicates": policy_duplicates}
        return {"recorded": len(events) - duplicates, "duplicates": duplicates,
                "episodes": len({row["subject"] for row in events}), "policies_recorded": policy_count}
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    minimum = 1 if str(cls).strip() == reading.CLASS else MIN_REASON_CHARS
    if len(reason) < minimum:
        raise ValueError("a decision needs a reason (one sentence)")
    reading_actor = str(cls).strip() == reading.CLASS and by in {"agent", "observed"}
    if by not in BY_VALUES and not reading_actor:
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
    if line["class"] in reading.CLASSES:
        record_reading([line], path)
        if line["class"] == reading.CLASS:
            line["features"] = {k: v for k, v in line["features"].items() if k != "policy"}
        return line
    elif by in {"agent", "observed"}:
        raise ValueError("agent/observed provenance is reserved for typed reading events")
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # One encoded blob, one write(2), under an exclusive lock. A ledger line
    # carries `features` (peers, evidence summary) and routinely exceeds the
    # 4096-byte limit below which O_APPEND alone is atomic, and the nightly's
    # set-default subprocess can run while an interactive resolve writes. A torn
    # line is invisible in both directions: `load` drops unparseable lines
    # silently, so corruption shows up as a precedent that quietly stopped
    # existing. This local fcntl lock is the sole writer boundary for the ledger.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _append_rows(fd, [line])
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


def load(path: Path | None = None, *, since: date | None = None,
         strict: bool = False) -> list[dict[str, Any]]:
    """Read decisions; reading storage uses strict mode to fail on torn/corrupt data."""
    target = path or ledger_path()
    if not target.is_file():
        return []
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeError as exc:
        if strict:
            raise ValueError("decision ledger has invalid UTF-8; repair required before reading access") from exc
        raise
    if strict and content and not content.endswith("\n"):
        raise ValueError("decision ledger has an incomplete tail; repair required before reading access")
    rows: list[dict[str, Any]] = []
    for number, raw in enumerate(content.splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError(f"decision ledger has invalid JSON at line {number}; repair required") from exc
            # Never silently shrink the ledger: a dropped line changes precedent
            # counts and the accuracy denominator with no diagnostic anywhere.
            sys.stderr.write(f"atelier: decision ledger skipped an unparseable line in {target}\n")
            continue
        if not isinstance(row, dict):
            if strict:
                raise ValueError(f"decision ledger has a non-object row at line {number}; repair required")
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

    Reading feedback has its own learning scope and does not refill permission
    to act on vault maintenance. Unobserved defaults remain unconfirmed.
    """
    heartbeat = max((str(r.get("ts", "")) for r in rows
                     if r.get("by") == "human" and r.get("class") not in reading.CLASSES), default="")
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
        if row_cls in reading.CLASSES:
            continue
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


def cmd_reading(args: argparse.Namespace) -> int:
    """Storage adapter for the typed reading episode contract."""
    target = Path(args.ledger) if args.ledger else None
    try:
        if args.command == "reading-policy":
            if args.id:
                if args.context or args.model is not None:
                    raise ValueError("--id cannot be combined with policy creation inputs")
                policies, _ = reading.policy_index(load(target, strict=True))
                if args.id not in policies:
                    raise ValueError("missing or conflicting policy snapshot")
                output = policies[args.id]
            else:
                if not args.model:
                    raise ValueError("policy creation requires --model")
                output = record_reading([policy_record(reading.policy_snapshot(args.file, args.context, args.model))], target)
        elif args.command == "reading-record":
            data = json.loads(args.input.read_text(encoding="utf-8"))
            batch = data if isinstance(data, list) else [data]
            rows = []
            for entry in batch:
                allowed = {"episode_id", "item_id", "policy_id", "evidence_ref", "schema", "event_id",
                           "item", "action", "policy", "event", "by", "reason", "source"}
                if not isinstance(entry, dict) or set(entry) - allowed:
                    raise ValueError("invalid reading event input fields")
                f = {key: entry[key] for key in ("episode_id", "item_id", "policy_id", "evidence_ref")}
                f.update(schema=entry.get("schema", 1), event_id=entry.get("event_id", str(uuid.uuid4())))
                for key in ("item", "action", "policy"):
                    if key in entry:
                        f[key] = entry[key]
                rows.append({"ts": _now(), "class": reading.CLASS,
                             "subject": reading.subject(f["episode_id"], f["item_id"]),
                             "verdict": entry["event"], "by": entry["by"], "reason": entry["reason"],
                             "features": f, "source": entry.get("source", "reading")})
            output = record_reading(rows, target)
        elif args.command == "reading-evaluate":
            output = reading.compare(*(json.loads(p.read_text(encoding="utf-8"))
                                       for p in (args.labels, args.baseline, args.candidate)))
        else:
            rows = load(target, strict=True)
            if args.command == "reading-evidence":
                output = reading.evidence(rows, limit=args.limit, item=args.item)
            elif args.command == "reading-outcomes":
                output = reading.outcomes(rows)
            elif args.command == "reading-episodes":
                output = reading.attribution(rows, item=args.item, limit=args.limit)
            else:
                cases, labels = reading.dataset(rows)
                output = cases if args.view == "cases" else labels
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
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

    p = sub.add_parser("reading-policy", help="Store a policy once, or export its exact snapshot by ID.")
    inputs = p.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--file", type=Path, action="append")
    inputs.add_argument("--id", help="Read-only export of a stored or legacy inline policy.")
    p.add_argument("--context", type=Path, action="append", default=[])
    p.add_argument("--model")
    p.set_defaults(func=cmd_reading)

    p = sub.add_parser("reading-record", help="Append one typed reading event or a JSON array; print a compact receipt.")
    p.add_argument("--input", type=Path, required=True)
    p.set_defaults(func=cmd_reading)

    p = sub.add_parser("reading-evidence", help="Explicit feedback and weaker consumption evidence.")
    p.add_argument("--item", help="Stable item identifier, e.g. readwise:DOC_ID")
    p.add_argument("--limit", type=int, default=50, help="Max entries per evidence group; omissions are counted.")
    p.set_defaults(func=cmd_reading)

    p = sub.add_parser("reading-outcomes", help="Descriptive policy outcomes with unknown counts.")
    p.set_defaults(func=cmd_reading)

    p = sub.add_parser("reading-episodes", help="Find selection attribution independently of taste evidence.")
    p.add_argument("--item", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_reading)

    p = sub.add_parser("reading-cases", help="Export a frozen rated case set or its separate labels.")
    p.add_argument("--view", choices=("cases", "labels"), default="cases")
    p.set_defaults(func=cmd_reading)

    p = sub.add_parser("reading-evaluate", help="Compare blind policy selections on the same rated cases.")
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.set_defaults(func=cmd_reading)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
