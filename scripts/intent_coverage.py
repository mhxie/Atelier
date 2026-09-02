#!/usr/bin/env python3
"""Deterministic intent matching, routing projection, and coverage logging.

Claude invokes command specs with ``/command`` and Codex invokes repo skills
with ``$command``. Their shared ``UserPromptSubmit`` hook projects the matched
``harness/intents.toml`` row into compact context for the live ``hi`` flow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import tomllib
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INTENTS_PATH = ROOT / "harness" / "intents.toml"

INTENT_MISS_FALLBACK_DIR = Path.home() / ".cache" / "atelier" / "intent_misses"
INTENT_MISS_KINDS = ("fallback", "ambiguous", "low_confidence")
INTENT_MISS_RUNTIMES = ("claude-code", "codex")
INTENT_MISS_DISTINCT_DAYS_THRESHOLD = 3
INTENT_MISS_KINDS_COL_WIDTH = len(",".join(sorted(INTENT_MISS_KINDS)))
INTENT_ROUTE_SCHEMA_VERSION = 2
INTENT_ROUTE_CONTEXT_PREFIX = "ATELIER_INTENT_ROUTE "
INTENT_ROUTE_MAX_CONTEXT_BYTES = 1024


def load_intents() -> dict[str, dict[str, Any]]:
    intents = _load_intents_canonical()
    overlay = ROOT / "harness" / "intents.local.toml"
    if overlay.is_file():
        try:
            with overlay.open("rb") as handle:
                local = tomllib.load(handle).get("intents", {})
        except (OSError, tomllib.TOMLDecodeError):
            return intents  # a broken overlay must never break routing
        for name, row in local.items():
            if not isinstance(row, dict) or name not in intents:
                continue  # overlay extends existing intents; it cannot invent new ones
            extra = [p for p in row.get("patterns", []) if isinstance(p, str)]
            merged = list(intents[name].get("patterns", []))
            merged.extend(p for p in extra if p not in merged)
            intents[name]["patterns"] = merged
    return intents


def _load_intents_canonical() -> dict[str, dict[str, Any]]:
    intents = load_table(INTENTS_PATH, "intents")
    if not isinstance(intents, dict):
        raise SystemExit("atelier: harness/intents.toml has no [intents] table")
    return intents


def match_intents(
    text: str, intents: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Match user text against intents.toml patterns.

    Substring match, case-insensitive. Returns matched intents sorted by
    descending priority. The fallback intent (empty patterns, priority 0) is
    included in results ONLY when no other intent matched, mirroring the
    "no specific intent matched" branch in hi.md.
    """
    text_lc = text.lower()
    matched: list[dict[str, Any]] = []
    fallback: dict[str, Any] | None = None
    for name, row in intents.items():
        if not isinstance(row, dict):
            continue
        patterns = row.get("patterns") or []
        priority = int(row.get("priority", 0))
        entry = {
            "name": name,
            "mode": str(row.get("mode", "")),
            "procedure": str(row.get("procedure", "")),
            "context_budget_bytes": int(row.get("context_budget_bytes", 0)),
            "agents": list(row.get("agents") or []),
            "profile_reads": list(row.get("profile_reads") or []),
            "priority": priority,
            "pattern": str(row.get("pattern", "")),
            "parallel": bool(row.get("parallel", False)),
            "expected_subagent_count": int(row.get("expected_subagent_count", 0)),
        }
        if not patterns:
            fallback = entry
            continue
        if not isinstance(patterns, list):
            continue
        hit = next(
            (p for p in patterns if isinstance(p, str) and p.lower() in text_lc), None
        )
        if hit:
            entry["matched_pattern"] = hit
            matched.append(entry)
    matched.sort(key=lambda e: -int(e["priority"]))
    if not matched and fallback is not None:
        fallback["matched_pattern"] = "<fallback: no patterns matched>"
        matched.append(fallback)
    return matched


def build_intent_route_projection(
    matches: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Project matcher output into the fields needed by the live ``hi`` flow.

    The projection contains registry data only. It never includes the raw user
    input, session metadata, or filesystem paths. Ambiguous top-priority rows
    are preserved so the orchestrator can clarify instead of trusting the
    stable-sort winner.
    """
    if not matches:
        return None

    def route_fields(match: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": str(match.get("name", "")),
            "mode": str(match.get("mode", "")),
            "procedure": str(match.get("procedure", "")),
            "context_budget_bytes": int(match.get("context_budget_bytes", 0)),
            "agents": list(match.get("agents") or []),
            "profile_reads": list(match.get("profile_reads") or []),
            "priority": int(match.get("priority", 0)),
            "matched_pattern": str(match.get("matched_pattern", "")),
            "parallel": bool(match.get("parallel", False)),
        }

    first = matches[0]
    top_priority = int(first.get("priority", 0))
    is_fallback = str(first.get("matched_pattern", "")).startswith("<fallback")
    top_matches = [
        match
        for match in matches
        if int(match.get("priority", 0)) == top_priority
        and not str(match.get("matched_pattern", "")).startswith("<fallback")
    ]
    projection: dict[str, Any] = {
        "schema": INTENT_ROUTE_SCHEMA_VERSION,
        "source": "harness/intents.toml",
        **route_fields(first),
        "fallback": is_fallback,
        "ambiguous": len(top_matches) > 1,
    }
    if projection["ambiguous"]:
        projection["tied_candidates"] = [route_fields(match) for match in top_matches]
    return projection


def _emit_intent_route_projection(projection: dict[str, Any]) -> None:
    """Inject one bounded route packet through the shared hook protocol."""
    context = INTENT_ROUTE_CONTEXT_PREFIX + json.dumps(
        projection,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(context.encode("utf-8")) > INTENT_ROUTE_MAX_CONTEXT_BYTES:
        return
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def load_table(path: Path, table: str) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if table not in data:
        raise SystemExit(f"atelier: {path.relative_to(ROOT)} has no [{table}] table")
    value = data[table]
    if not isinstance(value, dict):
        raise SystemExit(f"atelier: {path.relative_to(ROOT)} [{table}] is not a table")
    return value


def cmd_intent(args: argparse.Namespace) -> int:
    """Match user text against the shared intent router for diagnostics.

    Mirrors the substring + priority matcher hi.md describes. Returns the
    winning intent + its dispatch shape (mode, agents, parallel). When
    multiple non-fallback intents match (ambiguity), all winners are listed
    and the caller should ask for clarification.
    """
    text = " ".join(args.text).strip()
    if not text:
        raise SystemExit(
            "atelier: intent requires a text argument. Example: intent 'review my goals'"
        )
    intents = load_intents()
    matches = match_intents(text, intents)

    if not matches:
        # Shouldn't happen since fallback is included on empty, but defend.
        payload: dict[str, Any] = {
            "input": text,
            "matched": [],
            "ambiguous": False,
            "fallback": True,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"input: {text}\n(no intent matched and no fallback declared)")
        return 0

    is_fallback = matches[0].get("matched_pattern", "").startswith("<fallback")
    top_priority = int(matches[0]["priority"])
    top_matches = [
        m
        for m in matches
        if int(m["priority"]) == top_priority
        and not m.get("matched_pattern", "").startswith("<fallback")
    ]
    ambiguous = len(top_matches) > 1

    payload = {
        "input": text,
        "winner": matches[0]["name"],
        "mode": matches[0]["mode"],
        "procedure": matches[0]["procedure"],
        "context_budget_bytes": matches[0]["context_budget_bytes"],
        "agents": matches[0]["agents"],
        "parallel": matches[0]["parallel"],
        "profile_reads": matches[0]["profile_reads"],
        "matched_pattern": matches[0].get("matched_pattern", ""),
        "priority": top_priority,
        "ambiguous": ambiguous,
        "fallback": is_fallback,
        "all_matches": [
            {
                "name": m["name"],
                "mode": m["mode"],
                "procedure": m["procedure"],
                "context_budget_bytes": m["context_budget_bytes"],
                "priority": int(m["priority"]),
                "matched_pattern": m.get("matched_pattern", ""),
                "agents": m["agents"],
                "parallel": m["parallel"],
            }
            for m in matches
        ],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"input:    {text}")
    print(
        f"winner:   intents.{payload['winner']}  (priority {top_priority}, mode {payload['mode']})"
    )
    if payload["matched_pattern"]:
        print(f"matched:  {payload['matched_pattern']}")
    print(f"workflow: {payload['procedure']}")
    print(f"context:  {payload['context_budget_bytes']} bytes")
    if payload["agents"]:
        agent_list = ", ".join(payload["agents"])
        para = " (parallel)" if payload["parallel"] else " (sequential)"
        print(f"agents:   {agent_list}{para}")
    else:
        print("agents:   (none — script-driven or solo orchestrator)")
    if payload["profile_reads"]:
        print(f"profile:  {', '.join(payload['profile_reads'])}")
    if is_fallback:
        print()
        print("note: no specific patterns matched; entered the semantic handoff.")
        print("      clarify only when semantic routing is materially ambiguous.")
    if ambiguous:
        print()
        print(
            f"AMBIGUOUS: {len(top_matches)} intents at priority {top_priority} match this input:"
        )
        for m in top_matches:
            print(
                f"  - intents.{m['name']}  (pattern: {m.get('matched_pattern', '')}, mode: {m['mode']})"
            )
        print("ask the user which intent they meant before dispatching.")
    return 0


def resolve_intent_hit_dir() -> Path:
    """Sibling of the miss log; single high-confidence routes, hashed."""
    return resolve_intent_miss_dir().parent / "intent_hits"


def write_intent_hit(runtime: str, match: dict[str, Any], text: str) -> None:
    """One JSONL line per happy-path route: intent, pattern, sha256 of the
    normalized text. No raw text: this is a denominator, not a history."""
    import hashlib

    try:
        directory = resolve_intent_hit_dir()
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "runtime": runtime,
                "intent": match.get("name"),
                "matched_pattern": match.get("matched_pattern"),
                "text_sha256": hashlib.sha256(_normalize_phrase(text).encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
        )
        path = directory / f"{date.today().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # best-effort; never block a live invocation


def resolve_intent_miss_dir() -> Path:
    """Where intent-miss JSONL files live.

    Prefers `$OV/_meta/intent_misses/` when `$OV` is set (the durable Atelier
    location alongside `shadow_logs/`). Falls back to
    `~/.cache/atelier/intent_misses/` otherwise so tests / CI / fresh checkouts
    without `$OV` can still exercise the round trip.
    """
    ov = os.environ.get("OV")
    if ov:
        from _paths import tier_segments

        return Path(ov) / tier_segments().get("meta", "_meta") / "intent_misses"
    return INTENT_MISS_FALLBACK_DIR


def write_intent_miss(payload: dict[str, Any]) -> Path | None:
    """Append one JSONL line to today's intent-miss log.

    Returns the file path on success, or None on OSError. Never raises:
    miss logging is best-effort and must not block a live hi flow.
    """
    miss_dir = resolve_intent_miss_dir()
    try:
        miss_dir.mkdir(parents=True, exist_ok=True)
        log_file = miss_dir / f"{date.today().isoformat()}.jsonl"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return log_file
    except OSError:
        return None


def cmd_intent_log(args: argparse.Namespace) -> int:
    """Record an unclassified native hi invocation for coverage review.

    Called by the orchestrator after deciding routing. Three trigger cases
    (see `.claude/commands/hi.md` → "Miss Logging"):
      - fallback: `intents.general` won by default; nothing else matched.
      - ambiguous: 2+ non-fallback intents tied at the top priority.
      - low_confidence: a generic substring matched inside a longer message
        whose primary intent looked different; orchestrator used
        `AskUserQuestion` to confirm.
    """
    raw = args.input.strip()
    if not raw:
        sys.stderr.write("atelier: intent-log skipped (empty --input)\n")
        return 0
    try:
        priority_val: int | None = (
            int(args.initial_priority) if args.initial_priority is not None else None
        )
    except (TypeError, ValueError):
        sys.stderr.write(
            f"atelier: intent-log dropping --initial-priority (not an int: {args.initial_priority!r})\n"
        )
        priority_val = None
    payload: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "runtime": args.runtime,
        "raw_input": raw,
        "match_kind": args.match_kind,
        "initial_match": {
            "name": args.initial_name or None,
            "priority": priority_val,
            "matched_pattern": args.initial_pattern or None,
        },
    }
    if args.candidates:
        try:
            payload["ambiguity_candidates"] = json.loads(args.candidates)
        except json.JSONDecodeError as e:
            sys.stderr.write(
                f"atelier: intent-log dropping malformed --candidates (preserved as raw string): {e}\n"
            )
            payload["ambiguity_candidates_raw"] = args.candidates
    if args.clarified_to:
        payload["clarified_to"] = args.clarified_to
    if args.final_dispatch:
        payload["final_dispatch"] = args.final_dispatch
    if args.notes:
        payload["notes"] = args.notes

    path = write_intent_miss(payload)
    if path is None:
        sys.stderr.write(
            "atelier: intent-log write failed; skipped (best-effort, never blocks hi)\n"
        )
        return 0
    if not args.quiet:
        print(f"intent-log: {path}")
    return 0


def _intent_text_from_hook_prompt(prompt: str, runtime: str) -> str | None:
    """Extract the routed text from a Claude or Codex Atelier entry prompt.

    Claude Code submits `/hi <text>` or `/reflect <text>`. Codex submits the
    equivalent explicit skills as `$hi <text>` or `$reflect <text>`. An entry
    with no routed text opens the command's menu and therefore returns None.
    """
    stripped = prompt.strip()
    prefix = r"/(?:hi|reflect)" if runtime == "claude-code" else r"\$(?:hi|reflect)"
    match = re.fullmatch(
        rf"{prefix}(?:\s+(.+))?",
        stripped,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        text = (match.group(1) or "").strip()
        return text or None
    return None


def cmd_intent_hook(args: argparse.Namespace) -> int:
    """`UserPromptSubmit` hook entry: route projection plus miss capture.

    Reads the hook's stdin JSON (`prompt`, `session_id`, `transcript_path`,
    etc), detects the runtime's native Atelier entry shape, and injects a
    bounded projection of the deterministic registry match. It also auto-logs
    mechanically identifiable fallback or ambiguous branches.

    Cases the hook CANNOT classify (intentional carve-out — the orchestrator
    retains the in-band `intent-log` path for these):
      - `low_confidence`: heuristic over message shape; LLM judgment lives
        in `.claude/commands/hi.md` § Clarify before dispatching.
      - Post-clarification enrichment (`clarified_to`, `final_dispatch`):
        only known after `AskUserQuestion` resolves.

    Best-effort throughout: every failure path returns 0. Oversize route
    projections stay silent so the command can fall back to reading the full
    registry. A broken hook must never block a live command invocation.
    """
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    prompt = str(data.get("prompt", ""))
    user_text = _intent_text_from_hook_prompt(prompt, args.runtime)
    if user_text is None:
        return 0
    try:
        intents = load_intents()
        matches = match_intents(user_text, intents)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
        return 0
    if not matches:
        return 0
    projection = build_intent_route_projection(matches)
    if projection is None:
        return 0
    is_fallback = matches[0].get("matched_pattern", "").startswith("<fallback")
    top_priority = int(matches[0]["priority"])
    top_matches = [
        m
        for m in matches
        if int(m["priority"]) == top_priority
        and not m.get("matched_pattern", "").startswith("<fallback")
    ]
    is_ambiguous = len(top_matches) > 1
    if is_fallback or is_ambiguous:
        payload: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "runtime": args.runtime,
            "raw_input": user_text,
            "match_kind": "fallback" if is_fallback else "ambiguous",
            "initial_match": {
                "name": matches[0]["name"],
                "priority": top_priority,
                "matched_pattern": matches[0].get("matched_pattern", ""),
            },
            "logged_by": "user_prompt_submit_hook",
        }
        if is_ambiguous:
            payload["ambiguity_candidates"] = [
                {
                    "name": m["name"],
                    "priority": int(m["priority"]),
                    "matched_pattern": m.get("matched_pattern", ""),
                }
                for m in top_matches
            ]
        if data.get("session_id"):
            payload["session_id"] = str(data["session_id"])
        write_intent_miss(payload)
    if len(matches) == 1 and matches[0].get("matched_pattern") not in (None, "", "<fallback: no patterns matched>"):
        write_intent_hit(args.runtime, matches[0], user_text)

    _emit_intent_route_projection(projection)
    return 0


def _normalize_phrase(raw: Any) -> str:
    """Normalize a raw_input string for recurrence aggregation.

    NFKC unifies width / form differences (full-width vs half-width CJK
    punctuation, ligatures); collapsing whitespace + casefold makes
    `"improve  the repo"` and `"Improve The Repo"` aggregate together.
    Trailing length cap matches the original ≤200-char clamp. Punctuation
    is NOT stripped — `url.com` and `Yes.` should not collide.
    """
    s = unicodedata.normalize("NFKC", str(raw))
    return " ".join(s.split()).casefold()[:200]


def cmd_intent_misses(args: argparse.Namespace) -> int:
    """Aggregate the intent-miss log for batch coverage review.

    Use to spot phrases that recur often enough to become trigger candidates
    for an existing or new intent. Signal: same phrase logged on
    INTENT_MISS_DISTINCT_DAYS_THRESHOLD+ distinct file-dates → strong
    candidate for a `harness/intents.toml` pattern addition.
    """
    try:
        since = date.fromisoformat(args.since) if args.since else None
    except ValueError:
        raise SystemExit(
            f"atelier: --since must be YYYY-MM-DD (got {args.since!r})"
        ) from None
    miss_dir = resolve_intent_miss_dir()
    if not miss_dir.is_dir():
        if args.json:
            print(
                json.dumps(
                    {"events": [], "since": args.since, "miss_dir": str(miss_dir)}
                )
            )
        else:
            print(f"intent-misses: no log directory at {miss_dir}")
            print("Nothing logged yet. Directory is created on first miss.")
        return 0

    # Pair every event with the date of the file it came from. file_date is
    # the consumer-side ground truth for the "distinct days" coverage signal
    # AND for --since filtering — keeps both axes consistent, defending the
    # signal against TZ slips between writer wall-clock and event timestamps.
    events: list[tuple[date, dict[str, Any]]] = []
    for p in sorted(miss_dir.glob("*.jsonl")):
        try:
            file_date = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if since and file_date < since:
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    events.append((file_date, ev))
        except OSError:
            continue

    if args.match_kind:
        events = [(d, e) for (d, e) in events if e.get("match_kind") == args.match_kind]
    if args.runtime:
        events = [(d, e) for (d, e) in events if e.get("runtime") == args.runtime]

    kind_counts: dict[str, int] = {}
    phrase_stats: dict[str, dict[str, Any]] = {}
    empty_phrase_count = 0
    for file_date, ev in events:
        kind = str(ev.get("match_kind", "(unknown)"))
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        phrase = _normalize_phrase(ev.get("raw_input", ""))
        if not phrase:
            empty_phrase_count += 1
            continue
        entry = phrase_stats.setdefault(
            phrase,
            {
                "count": 0,
                "first_seen": None,
                "last_seen": None,
                "kinds": set(),
                "days": set(),
            },
        )
        entry["count"] += 1
        entry["kinds"].add(kind)
        entry["days"].add(file_date.isoformat())
        if isinstance(ev.get("clarified_to"), str) and ev["clarified_to"]:
            entry["clarified"] = ev["clarified_to"]
        ts = ev.get("timestamp")
        if isinstance(ts, str):
            if entry["first_seen"] is None or ts < entry["first_seen"]:
                entry["first_seen"] = ts
            if entry["last_seen"] is None or ts > entry["last_seen"]:
                entry["last_seen"] = ts

    if args.json:
        payload = {
            "since": args.since,
            "miss_dir": str(miss_dir),
            "total_events": len(events),
            "by_kind": kind_counts,
            "events_with_empty_phrase": empty_phrase_count,
            "proposal_threshold_days": INTENT_MISS_DISTINCT_DAYS_THRESHOLD,
            # Same rows the text mode prints under --propose, so a caller reading
            # JSON (the /triage overview) sees the same actionable set instead
            # of an always-empty lane.
            "proposals": [
                {
                    "phrase": phrase,
                    "target": pc.get("clarified") or None,
                    "count": pc["count"],
                    "distinct_days": len(pc["days"]),
                }
                for phrase, pc in sorted(
                    phrase_stats.items(), key=lambda kv: (-kv[1]["count"], kv[0])
                )
                if len(pc["days"]) >= INTENT_MISS_DISTINCT_DAYS_THRESHOLD
            ]
            if getattr(args, "propose", False)
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
                for phrase, pc in sorted(
                    phrase_stats.items(), key=lambda kv: (-kv[1]["count"], kv[0])
                )
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Intent miss log: {miss_dir}")
    print(
        f"Total events: {len(events)}"
        + (f"  (since {args.since})" if args.since else "")
    )
    print()
    print("By match kind:")
    if not kind_counts:
        print("  (none)")
    for k in sorted(kind_counts.keys()):
        print(f"  {k}: {kind_counts[k]}")
    print()
    if not phrase_stats:
        print("No phrases logged.")
        return 0
    sorted_phrases = sorted(
        phrase_stats.items(), key=lambda kv: (-kv[1]["count"], kv[0])
    )
    if empty_phrase_count:
        print(
            f"({empty_phrase_count} event(s) had empty raw_input — counted in by-kind totals, omitted from the phrase table below.)"
        )
    print(f"Top phrases (showing up to {args.top}):")
    col_w = INTENT_MISS_KINDS_COL_WIDTH
    print(f"  count  days  {'kinds'.ljust(col_w)}  phrase")
    for phrase, pc in sorted_phrases[: args.top]:
        kinds_str = ",".join(sorted(pc["kinds"])).ljust(col_w)
        days_str = f"{len(pc['days']):>4}"
        count_str = f"{pc['count']:>5}"
        print(f"  {count_str}  {days_str}  {kinds_str}  {phrase}")
    repeaters = [
        (phrase, pc)
        for phrase, pc in sorted_phrases
        if len(pc["days"]) >= INTENT_MISS_DISTINCT_DAYS_THRESHOLD
    ]
    if repeaters:
        print()
        print(
            f"Coverage signal: {len(repeaters)} phrase(s) recurred across "
            f"{INTENT_MISS_DISTINCT_DAYS_THRESHOLD}+ distinct days."
        )
        print("Consider adding a trigger to harness/intents.toml for these.")
    if getattr(args, "propose", False):
        print()
        if repeaters:
            print("# --- proposed overlay rows for harness/intents.local.toml (review before adopting) ---")
            for phrase, pc in repeaters:
                target = pc.get("clarified") or "<intent-name>"
                safe = phrase.replace("\\", "\\\\").replace('"', '\\"')
                print(f"# seen {len(pc['days'])} distinct days, {pc['count']} events; clarified_to={pc.get('clarified')}")
                print(f"[intents.{target}]")
                print(f'patterns = ["{safe}"]')
                print()
        else:
            print("# no phrases cleared the distinct-days threshold; nothing to propose")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/intent_coverage.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Inspect and log Atelier hi intent coverage for Claude `/hi` and Codex `$hi`.",
        epilog=textwrap.dedent(
            """\
            Examples:
              python3 scripts/intent_coverage.py intent "review my goals"
              python3 scripts/intent_coverage.py intent "https://arxiv.org/abs/2501.12345"
              python3 scripts/intent_coverage.py intent "5/4 早上去了 X" --json
              python3 scripts/intent_coverage.py intent-log --input "improve the repo" \\
                --match-kind fallback --runtime claude-code \\
                --initial-name reflection --initial-priority 0 \\
                --initial-pattern "<fallback>" --final-dispatch "engineering-task"
              python3 scripts/intent_coverage.py intent-misses --since 2026-05-01
            """
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    intent = sub.add_parser(
        "intent",
        help="Inspect text against the shared hi intent rules.",
        description=(
            "Diagnostic for harness/intents.toml. Runs the substring-and-priority "
            "matcher described by hi.md and reports the matched intent (or AMBIGUOUS "
            "when multiple priority-tied intents match). Interactive command execution "
            "does not use this utility."
        ),
    )
    intent.add_argument(
        "text", nargs="+", help="User text to match against intent patterns."
    )
    intent.add_argument("--json", action="store_true", help="Emit JSON.")
    intent.set_defaults(func=cmd_intent)

    intent_log = sub.add_parser(
        "intent-log",
        help="Record an unclassified native hi invocation for batch coverage review.",
        description=(
            "Append one JSONL line to $OV/_meta/intent_misses/YYYY-MM-DD.jsonl "
            "(falls back to ~/.cache/atelier/intent_misses/ when $OV is unset). "
            "Call from the orchestrator after a native hi invocation that fell back, "
            "was ambiguous, or was clarified due to low confidence. "
            "See protocols/intent-coverage.md."
        ),
    )
    intent_log.add_argument(
        "--input",
        required=True,
        help="Raw text following Claude /hi or Codex $hi.",
    )
    intent_log.add_argument(
        "--match-kind",
        required=True,
        choices=INTENT_MISS_KINDS,
        help="Why this counted as a miss.",
    )
    intent_log.add_argument(
        "--runtime",
        required=True,
        choices=INTENT_MISS_RUNTIMES,
        help="Which orchestrator runtime logged the miss.",
    )
    intent_log.add_argument(
        "--initial-name",
        default=None,
        help="Name of the initial matched intent (e.g., 'general' for fallback).",
    )
    intent_log.add_argument(
        "--initial-priority", default=None, help="Priority of the initial match."
    )
    intent_log.add_argument(
        "--initial-pattern",
        default=None,
        help="Pattern that matched (or '<fallback>' for the fallback case).",
    )
    intent_log.add_argument(
        "--candidates",
        default=None,
        help=(
            "For ambiguous: JSON array of {name, priority, matched_pattern}. "
            "Key name matches `intent --json` output verbatim — pass the matcher's "
            "objects straight through without renaming."
        ),
    )
    intent_log.add_argument(
        "--clarified-to",
        default=None,
        help="Intent name the user picked from the clarification menu.",
    )
    intent_log.add_argument(
        "--final-dispatch",
        default=None,
        help="What was actually dispatched (intent name, or free-text label like 'engineering-task').",
    )
    intent_log.add_argument(
        "--notes",
        default=None,
        help="Free-text orchestrator note about why this was a miss.",
    )
    intent_log.add_argument(
        "--quiet", action="store_true", help="Don't print the appended path on success."
    )
    intent_log.set_defaults(func=cmd_intent_log)

    intent_misses = sub.add_parser(
        "intent-misses",
        help="Aggregate the intent-miss log for batch coverage review.",
        description=(
            "Print counts by match_kind and the top distinct phrases from the "
            "intent-miss log. Phrases recurring across 3+ distinct days are "
            "flagged as candidate triggers for harness/intents.toml."
        ),
    )
    intent_misses.add_argument(
        "--since",
        help=(
            "YYYY-MM-DD; only include events from this date forward. "
            "Filter applies at FILE-DATE granularity (the log file's filename "
            "date), not at event-timestamp granularity — a TZ-skewed event "
            "near midnight is grouped with its file's date."
        ),
    )
    intent_misses.add_argument(
        "--match-kind",
        choices=INTENT_MISS_KINDS,
        help="Filter to one match_kind (vocabulary matches intent-log --match-kind).",
    )
    intent_misses.add_argument(
        "--runtime", choices=INTENT_MISS_RUNTIMES, help="Filter to one runtime."
    )
    intent_misses.add_argument(
        "--top",
        type=int,
        default=20,
        help="Top-N distinct phrases to display (default 20).",
    )
    intent_misses.add_argument("--json", action="store_true", help="Emit JSON.")
    intent_misses.add_argument("--propose", action="store_true", help="Emit candidate overlay rows for harness/intents.local.toml")
    intent_misses.set_defaults(func=cmd_intent_misses)

    intent_hook = sub.add_parser(
        "intent-hook",
        help="UserPromptSubmit hook entry: compact route projection plus miss capture.",
        description=(
            "Wire as a Claude Code or Codex UserPromptSubmit hook command. Reads the "
            "hook's stdin JSON, detects the runtime's hi/reflect entry shape, runs the deterministic "
            "matcher, injects a bounded registry projection for the live command, "
            "and auto-logs fallback/ambiguous to "
            "$OV/_meta/intent_misses/YYYY-MM-DD.jsonl. "
            "Best-effort: every failure returns 0."
        ),
    )
    intent_hook.add_argument(
        "--runtime",
        required=True,
        choices=INTENT_MISS_RUNTIMES,
        help="Which orchestrator runtime is firing this hook.",
    )
    intent_hook.set_defaults(func=cmd_intent_hook)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
