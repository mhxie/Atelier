#!/usr/bin/env python3
"""Intent catalog, route logging, and coverage review for `/hi` and `$hi`.

Routing is model judgment: the orchestrator reads the catalog emitted by
``catalog`` (one line per ``harness/intents.toml`` row), picks the row whose
``description`` fits the request, and executes that row's ``procedure``.
Nothing here matches text. This module owns the catalog projection, the
per-route ledger written by ``intent-log``, and the ``intent-misses`` review
that turns recurring unrouted requests into catalog work.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import tomllib
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INTENTS_PATH = ROOT / "harness" / "intents.toml"

ROUTE_LOG_FALLBACK_DIR = Path.home() / ".cache" / "atelier" / "intent_routes"
ROUTE_KINDS = ("routed", "general", "clarified", "corrected")
PRIVATE_ROW_DEFAULTS = {
    "mode": "private-feature",
    "context_budget_bytes": 8192,
    "agents": [],
    "profile_reads": [],
    "pattern": "solo",
    "expected_subagent_count": 0,
    "parallel": False,
}
LEGACY_MISS_KINDS = ("fallback", "ambiguous", "low_confidence")
INTENT_MISS_KINDS = ROUTE_KINDS + LEGACY_MISS_KINDS
INTENT_MISS_RUNTIMES = ("claude-code", "codex")
INTENT_MISS_DISTINCT_DAYS_THRESHOLD = 3
INTENT_MISS_KINDS_COL_WIDTH = max(len(k) for k in INTENT_MISS_KINDS)
FALLBACK_INTENT = "general"


def load_table(path: Path, table: str) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if table not in data:
        raise SystemExit(f"atelier: {path.relative_to(ROOT)} has no [{table}] table")
    value = data[table]
    if not isinstance(value, dict):
        raise SystemExit(f"atelier: {path.relative_to(ROOT)} [{table}] is not a table")
    return value


def _load_intents_canonical() -> dict[str, dict[str, Any]]:
    """The validated shared loader; tests may point ROOT at a copied harness."""
    from registries import RegistryError, load_intents as _load

    try:
        return _load(ROOT)
    except RegistryError as exc:
        raise SystemExit(f"atelier: {exc}") from exc


def resolve_private_procedure(value: Any) -> Path | None:
    """Where a private row's procedure lives: absolute, `$OV`-relative, or
    `<paths.private_features>`-relative (so `my-feature/SKILL.md` works)."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = Path(value.strip()).expanduser()
    candidates = [raw] if raw.is_absolute() else []
    ov = os.environ.get("OV")
    if ov and not raw.is_absolute():
        candidates.append(Path(ov) / raw)
        try:
            from _paths import tier_segments

            candidates.append(Path(ov) / tier_segments().get("private_features", "_tools/features") / raw)
        except Exception:  # noqa: BLE001  (registry problems must not break routing)
            pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def validate_private_row(name: str, row: Any) -> list[str]:
    """Problems that make an overlay-only row unusable (empty = accepted)."""
    problems: list[str] = []
    if not isinstance(row, dict):
        return [f"{name}: not a table"]
    if not isinstance(row.get("description"), str) or not row["description"].strip():
        problems.append(f"{name}: needs a one-line description")
    if resolve_private_procedure(row.get("procedure")) is None:
        problems.append(f"{name}: procedure must be an existing file (absolute, $OV-relative, or under the private features tier)")
    examples = row.get("examples", [])
    if not isinstance(examples, list) or any(not isinstance(e, str) for e in examples):
        problems.append(f"{name}: examples must be a list of strings")
    return problems


def merge_overlay(
    intents: dict[str, dict[str, Any]], overlay: Path
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Apply `intents.local.toml`: extra ``examples`` on canonical rows, and
    private rows (description + procedure) the public catalog must not carry.

    A canonical row's other fields cannot be overridden. A broken overlay or
    an invalid private row never breaks routing; problems are returned.
    Legacy overlays that still say ``patterns`` are read as ``examples``.
    """
    problems: list[str] = []
    if not overlay.is_file():
        return intents, problems
    try:
        with overlay.open("rb") as handle:
            local = tomllib.load(handle).get("intents", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return intents, [f"overlay unreadable: {exc}"]
    if not isinstance(local, dict):
        return intents, ["overlay [intents] is not a table"]
    for name, row in local.items():
        if not isinstance(row, dict):
            problems.append(f"{name}: not a table")
            continue
        if name in intents:
            extra = row.get("examples", row.get("patterns", []))
            if not isinstance(extra, list):
                problems.append(f"{name}: examples must be a list")
                continue
            merged = [e for e in intents[name].get("examples", []) if isinstance(e, str)]
            merged.extend(e for e in extra if isinstance(e, str) and e not in merged)
            intents[name]["examples"] = merged
            continue
        row_problems = validate_private_row(name, row)
        if row_problems:
            problems.extend(row_problems)
            continue
        private = dict(PRIVATE_ROW_DEFAULTS)
        private.update({k: v for k, v in row.items() if k in PRIVATE_ROW_DEFAULTS or k in ("description", "examples", "procedure")})
        private["procedure"] = str(resolve_private_procedure(row["procedure"]))
        private["private"] = True
        intents[name] = private
    return intents, problems


def overlay_path() -> Path:
    return ROOT / "harness" / "intents.local.toml"


def load_intents() -> dict[str, dict[str, Any]]:
    """Canonical rows merged with the gitignored overlay (see merge_overlay)."""
    intents, problems = merge_overlay(_load_intents_canonical(), overlay_path())
    for problem in problems:
        sys.stderr.write(f"atelier: intents.local.toml skipped: {problem}\n")
    return intents


def catalog_rows(intents: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The fields the orchestrator needs to classify and announce a route."""
    rows: list[dict[str, Any]] = []
    for name, row in intents.items():
        rows.append(
            {
                "name": name,
                "description": str(row.get("description", "")).strip(),
                "mode": str(row.get("mode", "")),
                "procedure": str(row.get("procedure", "")),
                "agents": [a for a in (row.get("agents") or []) if isinstance(a, str)],
                "profile_reads": [
                    p for p in (row.get("profile_reads") or []) if isinstance(p, str)
                ],
                "parallel": bool(row.get("parallel", False)),
                "examples": [e for e in (row.get("examples") or []) if isinstance(e, str)],
                "private": bool(row.get("private", False)),
            }
        )
    return rows


def render_catalog(rows: list[dict[str, Any]], *, examples: bool) -> str:
    """One line per intent: ``name: description`` plus a compact dispatch tag."""
    lines: list[str] = []
    for row in rows:
        agents = ", ".join(row["agents"]) or "-"
        tag = f"[{agents}{' (parallel)' if row['parallel'] else ''}; {row['procedure']}]"
        marker = " (private)" if row["private"] else ""
        lines.append(f"{row['name']}{marker}: {row['description']} {tag}")
        if examples and row["examples"]:
            lines.append("  e.g. " + " | ".join(row["examples"]))
    return "\n".join(lines) + "\n"


def cmd_catalog(args: argparse.Namespace) -> int:
    rows = catalog_rows(load_intents())
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    sys.stdout.write(render_catalog(rows, examples=args.examples))
    return 0


def resolve_route_log_dir() -> Path:
    """Where per-route JSONL files live.

    Prefers `$OV/_meta/intent_routes/` when `$OV` is set. Falls back to
    `~/.cache/atelier/intent_routes/` so tests and fresh checkouts without
    `$OV` still exercise the round trip.
    """
    ov = os.environ.get("OV")
    if ov:
        from _paths import tier_segments

        return Path(ov) / tier_segments().get("meta", "_meta") / "intent_routes"
    return ROUTE_LOG_FALLBACK_DIR


def resolve_intent_miss_dir() -> Path:
    """Legacy miss-only log written by the retired substring router."""
    return resolve_route_log_dir().parent / "intent_misses"


def route_log_dirs() -> list[Path]:
    """Directories the review reads: the live route ledger plus legacy misses."""
    return [resolve_route_log_dir(), resolve_intent_miss_dir()]


def write_route_event(payload: dict[str, Any]) -> Path | None:
    """Append one JSONL line to today's route log.

    Returns the file path on success, or None on OSError. Never raises:
    logging is best-effort and must not block a live `/hi` flow.
    """
    log_dir = resolve_route_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{date.today().isoformat()}.jsonl"
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return log_file
    except OSError:
        return None


def cmd_intent_log(args: argparse.Namespace) -> int:
    """Record one `/hi` route decision.

    Called by the orchestrator after routing, on every contextual invocation:
      - routed: one catalog row fit with confidence.
      - general: nothing fit; `intents.general` handed the request off
        (`--final-dispatch` names what actually ran when known).
      - clarified: the orchestrator asked the user to pick among candidates.
      - corrected: a confident route the user redirected after the
        announcement; `--intent` is the wrong row, `--clarified-to` the
        right one. This is the false-hit signal.
    """
    raw = args.input.strip()
    if not raw:
        sys.stderr.write("atelier: intent-log skipped (empty --input)\n")
        return 0
    payload: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "runtime": args.runtime,
        "raw_input": raw,
        "match_kind": args.match_kind,
        "intent": args.intent or (FALLBACK_INTENT if args.match_kind == "general" else None),
    }
    if args.candidates:
        candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
        if candidates:
            payload["candidates"] = candidates
    if args.clarified_to:
        payload["clarified_to"] = args.clarified_to
    if args.final_dispatch:
        payload["final_dispatch"] = args.final_dispatch
    if args.notes:
        payload["notes"] = args.notes

    path = write_route_event(payload)
    if path is None:
        sys.stderr.write(
            "atelier: intent-log write failed; skipped (best-effort, never blocks hi)\n"
        )
        return 0
    if args.match_kind in ("clarified", "corrected") and args.clarified_to:
        # A clarification or correction is a human routing decision: it
        # enters the ledger so the precedent judge can learn what "this
        # kind of request" means, and so false hits have a record.
        import decisions

        if args.match_kind == "clarified":
            reason = args.notes or f"user chose {args.clarified_to} over {', '.join(payload.get('candidates', [])) or 'the offered rows'}"
        else:
            reason = args.notes or f"user redirected {args.intent or 'the announced route'} to {args.clarified_to}"
        decisions.record_best_effort(
            cls="hi/route",
            subject=_normalize_phrase(raw),
            verdict=f"{args.match_kind}:{args.clarified_to}",
            reason=reason,
            features={"candidates": payload.get("candidates", []), "announced": args.intent, "runtime": args.runtime},
            source="hi",
            by="human",
        )
    if not args.quiet:
        print(f"intent-log: {path}")
    return 0


def _normalize_phrase(raw: Any) -> str:
    """Normalize a raw_input string for recurrence aggregation.

    NFKC unifies width and form differences; collapsing whitespace plus
    casefold makes `"improve  the repo"` and `"Improve The Repo"` aggregate
    together. Punctuation is kept so `url.com` and `Yes.` do not collide.
    """
    s = unicodedata.normalize("NFKC", str(raw))
    return " ".join(s.split()).casefold()[:200]


def is_miss(event: dict[str, Any]) -> bool:
    """Every kind except a confident route counts as a coverage miss."""
    return str(event.get("match_kind", "")) != "routed"


def load_route_events(
    since: date | None = None, dirs: list[Path] | None = None
) -> list[tuple[date, dict[str, Any]]]:
    """Every (file_date, event) pair across the route ledger and legacy misses.

    file_date is the consumer-side ground truth for `--since` and for the
    distinct-days signal, which keeps both axes consistent against TZ slips
    between writer wall-clock and event timestamps.
    """
    events: list[tuple[date, dict[str, Any]]] = []
    for log_dir in dirs if dirs is not None else route_log_dirs():
        if not log_dir.is_dir():
            continue
        for path in sorted(log_dir.glob("*.jsonl")):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if since and file_date < since:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append((file_date, event))
    return events


def _proposal_target(stats: dict[str, Any]) -> str | None:
    return stats.get("clarified") or stats.get("dispatched") or None


def cmd_intent_misses(args: argparse.Namespace) -> int:
    """Aggregate unrouted `/hi` requests for catalog review.

    A phrase logged as general or clarified on
    INTENT_MISS_DISTINCT_DAYS_THRESHOLD+ distinct days is a recurring gap:
    sharpen a row's description, add an example, or write a new procedure.
    """
    try:
        since = date.fromisoformat(args.since) if args.since else None
    except ValueError:
        raise SystemExit(
            f"atelier: --since must be YYYY-MM-DD (got {args.since!r})"
        ) from None
    dirs = route_log_dirs()
    if not any(d.is_dir() for d in dirs):
        if args.json:
            print(json.dumps({"events": [], "since": args.since, "log_dirs": [str(d) for d in dirs]}))
        else:
            print(f"intent-misses: no route log under {dirs[0].parent}")
            print("Nothing logged yet. The directory is created on the first `/hi` route.")
        return 0

    events = load_route_events(since, dirs)
    if args.match_kind:
        events = [(d, e) for (d, e) in events if e.get("match_kind") == args.match_kind]
    if args.runtime:
        events = [(d, e) for (d, e) in events if e.get("runtime") == args.runtime]

    kind_counts: dict[str, int] = {}
    routed_count = 0
    phrase_stats: dict[str, dict[str, Any]] = {}
    empty_phrase_count = 0
    for file_date, event in events:
        kind = str(event.get("match_kind", "(unknown)"))
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if not is_miss(event):
            routed_count += 1
            continue
        phrase = _normalize_phrase(event.get("raw_input", ""))
        if not phrase:
            empty_phrase_count += 1
            continue
        entry = phrase_stats.setdefault(
            phrase,
            {"count": 0, "first_seen": None, "last_seen": None, "kinds": set(), "days": set()},
        )
        entry["count"] += 1
        entry["kinds"].add(kind)
        entry["days"].add(file_date.isoformat())
        if isinstance(event.get("clarified_to"), str) and event["clarified_to"]:
            entry["clarified"] = event["clarified_to"]
        if isinstance(event.get("final_dispatch"), str) and event["final_dispatch"]:
            entry["dispatched"] = event["final_dispatch"]
        ts = event.get("timestamp")
        if isinstance(ts, str):
            if entry["first_seen"] is None or ts < entry["first_seen"]:
                entry["first_seen"] = ts
            if entry["last_seen"] is None or ts > entry["last_seen"]:
                entry["last_seen"] = ts

    sorted_phrases = sorted(phrase_stats.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    repeaters = [
        (phrase, pc)
        for phrase, pc in sorted_phrases
        if len(pc["days"]) >= INTENT_MISS_DISTINCT_DAYS_THRESHOLD
    ]
    miss_count = len(events) - routed_count

    if args.json:
        payload = {
            "since": args.since,
            "log_dirs": [str(d) for d in dirs],
            "total_events": len(events),
            "routed_events": routed_count,
            "miss_events": miss_count,
            "by_kind": kind_counts,
            "events_with_empty_phrase": empty_phrase_count,
            "proposal_threshold_days": INTENT_MISS_DISTINCT_DAYS_THRESHOLD,
            "proposals": [
                {
                    "phrase": phrase,
                    "target": _proposal_target(pc),
                    "count": pc["count"],
                    "distinct_days": len(pc["days"]),
                }
                for phrase, pc in repeaters
            ]
            if args.propose
            else None,
            "phrases": [
                {
                    "phrase": phrase,
                    "count": pc["count"],
                    "distinct_days": len(pc["days"]),
                    "first_seen": pc["first_seen"],
                    "last_seen": pc["last_seen"],
                    "kinds": sorted(pc["kinds"]),
                }
                for phrase, pc in sorted_phrases
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Route log: {dirs[0]} (+ legacy {dirs[1].name}/)")
    print(
        f"Total events: {len(events)}  routed: {routed_count}  misses: {miss_count}"
        + (f"  (since {args.since})" if args.since else "")
    )
    print()
    print("By match kind:")
    if not kind_counts:
        print("  (none)")
    for kind in sorted(kind_counts):
        print(f"  {kind}: {kind_counts[kind]}")
    print()
    if not phrase_stats:
        print("No unrouted phrases logged.")
        return 0
    if empty_phrase_count:
        print(
            f"({empty_phrase_count} miss event(s) had empty raw_input; counted above, omitted below.)"
        )
    print(f"Top unrouted or corrected phrases (showing up to {args.top}):")
    col_w = INTENT_MISS_KINDS_COL_WIDTH
    print(f"  count  days  {'kinds'.ljust(col_w)}  phrase")
    for phrase, pc in sorted_phrases[: args.top]:
        kinds_str = ",".join(sorted(pc["kinds"])).ljust(col_w)
        print(f"  {pc['count']:>5}  {len(pc['days']):>4}  {kinds_str}  {phrase}")
    if repeaters:
        print()
        print(
            f"Coverage signal: {len(repeaters)} phrase(s) recurred across "
            f"{INTENT_MISS_DISTINCT_DAYS_THRESHOLD}+ distinct days."
        )
    if args.propose:
        print()
        if not repeaters:
            print("# no phrases cleared the distinct-days threshold; nothing to propose")
            return 0
        print("# --- recurring unrouted requests (review before acting) ---")
        print("# Per phrase: sharpen the target row's description, add the phrase as an")
        print("# example in harness/intents.local.toml, or write a new procedure + row.")
        for phrase, pc in repeaters:
            target = _proposal_target(pc) or "<intent-name>"
            safe = phrase.replace("\\", "\\\\").replace('"', '\\"')
            print(
                f"# seen {len(pc['days'])} distinct days, {pc['count']} events; "
                f"target={_proposal_target(pc)}"
            )
            print(f"[intents.{target}]")
            print(f'examples = ["{safe}"]')
            print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/intent_coverage.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Intent catalog, route ledger, and coverage review for Claude `/hi` and Codex `$hi`.",
        epilog=textwrap.dedent(
            """\
            Examples:
              python3 scripts/intent_coverage.py catalog
              python3 scripts/intent_coverage.py intent-log --input "review my goals" \\
                --match-kind routed --runtime claude-code --intent review --quiet
              python3 scripts/intent_coverage.py intent-log --input "improve the repo" \\
                --match-kind general --runtime claude-code --final-dispatch engineering-task
              python3 scripts/intent_coverage.py intent-misses --since 2026-05-01 --propose
            """
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    catalog = sub.add_parser(
        "catalog",
        help="Print the intent catalog the orchestrator classifies against.",
        description=(
            "One line per harness/intents.toml row: name, description, and a "
            "compact dispatch tag. This is the routing input for /hi and $hi."
        ),
    )
    catalog.add_argument("--json", action="store_true", help="Emit JSON rows.")
    catalog.add_argument(
        "--examples", action="store_true", help="Include example phrases under each row."
    )
    catalog.set_defaults(func=cmd_catalog)

    intent_log = sub.add_parser(
        "intent-log",
        help="Record one /hi route decision.",
        description=(
            "Append one JSONL line to $OV/_meta/intent_routes/YYYY-MM-DD.jsonl "
            "(falls back to ~/.cache/atelier/intent_routes/ when $OV is unset). "
            "Call after every contextual /hi or $hi route. See protocols/intent-coverage.md."
        ),
    )
    intent_log.add_argument("--input", required=True, help="Raw text following /hi or $hi.")
    intent_log.add_argument(
        "--match-kind",
        required=True,
        choices=ROUTE_KINDS,
        help="routed (confident), general (handoff), clarified (asked the user), corrected (user redirected a confident route).",
    )
    intent_log.add_argument(
        "--runtime", required=True, choices=INTENT_MISS_RUNTIMES, help="Which runtime routed."
    )
    intent_log.add_argument(
        "--intent",
        default=None,
        help="Selected intent name (defaults to 'general' for --match-kind general).",
    )
    intent_log.add_argument(
        "--candidates",
        default=None,
        help="For clarified: comma-separated intent names offered to the user.",
    )
    intent_log.add_argument(
        "--clarified-to", default=None, help="Intent name the user picked."
    )
    intent_log.add_argument(
        "--final-dispatch",
        default=None,
        help="What actually ran (intent name, or a free-text label like 'engineering-task').",
    )
    intent_log.add_argument("--notes", default=None, help="Free-text orchestrator note.")
    intent_log.add_argument(
        "--quiet", action="store_true", help="Don't print the appended path on success."
    )
    intent_log.set_defaults(func=cmd_intent_log)

    intent_misses = sub.add_parser(
        "intent-misses",
        help="Aggregate unrouted /hi requests for catalog review.",
        description=(
            "Print counts by match_kind and the top unrouted phrases from the route "
            "ledger (plus the legacy miss log). Phrases recurring across 3+ distinct "
            "days are flagged as catalog work."
        ),
    )
    intent_misses.add_argument(
        "--since",
        help="YYYY-MM-DD; include events from this file date forward (file-date granularity).",
    )
    intent_misses.add_argument(
        "--match-kind", choices=INTENT_MISS_KINDS, help="Filter to one match_kind."
    )
    intent_misses.add_argument(
        "--runtime", choices=INTENT_MISS_RUNTIMES, help="Filter to one runtime."
    )
    intent_misses.add_argument(
        "--top", type=int, default=20, help="Top-N unrouted phrases to display (default 20)."
    )
    intent_misses.add_argument("--json", action="store_true", help="Emit JSON.")
    intent_misses.add_argument(
        "--propose",
        action="store_true",
        help="List recurring unrouted phrases as candidate catalog work.",
    )
    intent_misses.set_defaults(func=cmd_intent_misses)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
