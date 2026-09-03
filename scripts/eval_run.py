#!/usr/bin/env python3
"""Run the harness eval suite and record a scored snapshot.

The architecture review's core evolvability finding: nothing measured whether
the system got better, so neither a protocol edit nor a model upgrade could
be shown to help. This runner produces a comparable JSON per (date, git
SHA), written to `$OV/_meta/evals/`, so `/system-review` and the
`eval_regression` cue can diff consecutive runs.

Components (each skips cleanly when its substrate is unavailable):
  routing    route coverage over the last ROUTE_WINDOW_DAYS of the `/hi`
             ledger ($OV/_meta/intent_routes/ plus the legacy miss log):
             score = confident routes / all routes. Correctness of a route
             is not measured here; tests/fixtures/routing_evalset.json is
             the seed for a future judged routing eval.
  semantic   delegates to scripts/semantic_eval.py when the gold set and a
             Lance index exist; records Recall@5/MRR@10 style metrics.
  judged     model-judged routing: a subagent (sonnet by default) classifies
             tests/fixtures/routing_evalset.json against the catalog by
             description alone and writes its verdict JSON; pass that file
             with --judged-routing to fold it into the snapshot. The prompt
             and verdict shape live in protocols/intent-coverage.md. Recorded
             as skipped when no verdict is supplied.

Usage: uv run scripts/eval_run.py [--json] [--no-semantic] [--judged-routing VERDICT.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier_segments, vault_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "routing_evalset.json"
ROUTE_WINDOW_DAYS = 30


def eval_routing(today: date | None = None) -> dict:
    """Route coverage: share of `/hi` routes that hit a catalog row with confidence."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import intent_coverage as ic

    today = today or date.today()
    events = ic.load_route_events(since=today - timedelta(days=ROUTE_WINDOW_DAYS))
    if not events:
        return {"skipped": f"no /hi routes logged in {ROUTE_WINDOW_DAYS}d"}
    by_kind: dict[str, int] = {}
    misses: dict[str, int] = {}
    for _, event in events:
        kind = str(event.get("match_kind", "(unknown)"))
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if ic.is_miss(event):
            target = str(event.get("clarified_to") or event.get("final_dispatch") or "-")
            misses[target] = misses.get(target, 0) + 1
    routed = by_kind.get("routed", 0)
    corrected = by_kind.get("corrected", 0)
    return {
        "metric": "route_coverage",
        "window_days": ROUTE_WINDOW_DAYS,
        "cases": len(events),
        "passed": routed,
        "score": round(routed / len(events), 3),
        "corrected": corrected,
        "false_hit_rate": round(corrected / routed, 3) if routed else None,
        "by_kind": by_kind,
        "misses": [{"target": k, "count": v} for k, v in sorted(misses.items(), key=lambda kv: -kv[1])],
    }


def eval_semantic() -> dict:
    gold = ROOT / "scripts" / "_evalset.json"
    if not gold.is_file():
        return {"skipped": "no gold set"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "semantic_eval.py"), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        return {"skipped": f"semantic_eval exit {result.returncode}: {result.stderr.strip()[:160]}"}
    try:
        return {"metrics": json.loads(result.stdout)}
    except json.JSONDecodeError:
        return {"skipped": "semantic_eval emitted non-JSON"}


def load_judged_routing(path: Path | None) -> dict:
    """Validate a subagent's routing verdict file into the snapshot shape."""
    if path is None:
        return {"skipped": "no --judged-routing verdict supplied"}
    try:
        verdict = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"skipped": f"verdict unreadable: {exc!r}"}
    cases = verdict.get("cases")
    passed = verdict.get("passed")
    if not isinstance(cases, int) or not isinstance(passed, int) or cases <= 0 or not 0 <= passed <= cases:
        return {"skipped": "verdict needs integer cases and passed with 0 <= passed <= cases"}
    fixture_cases = len(json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"])
    result = {
        "metric": "judged_route_accuracy",
        "model": str(verdict.get("model", "unknown")),
        "cases": cases,
        "passed": passed,
        "score": round(passed / cases, 3),
        "misses": [m for m in verdict.get("misses", []) if isinstance(m, dict)],
        "catalog_bytes": verdict.get("catalog_bytes"),
    }
    if cases != fixture_cases:
        result["note"] = f"verdict covers {cases} cases; fixture has {fixture_cases}"
    return result


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
        capture_output=True, text=True, timeout=30, check=False,
    )
    return result.stdout.strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-semantic", action="store_true", help="Skip the (slow) retrieval eval.")
    parser.add_argument(
        "--judged-routing",
        type=Path,
        default=None,
        help="Verdict JSON written by the routing-judge subagent (see protocols/intent-coverage.md).",
    )
    args = parser.parse_args(argv)

    snapshot = {
        "date": date.today().isoformat(),
        "git_sha": _git_sha(),
        "routing": eval_routing(),
        "semantic": {"skipped": "--no-semantic"} if args.no_semantic else eval_semantic(),
        "judged": {"routing": load_judged_routing(args.judged_routing)},
    }

    out_dir = vault_root() / tier_segments().get("meta", "_meta") / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{snapshot['date']}-{snapshot['git_sha']}.json"
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    else:
        r = snapshot["routing"]
        if "skipped" in r:
            print(f"routing: skipped: {r['skipped']}")
        else:
            print(f"routing: {r['passed']}/{r['cases']} routed with confidence ({r['score']:.0%}, {r['window_days']}d)")
        sem = snapshot["semantic"]
        print(f"semantic: {'skipped: ' + sem['skipped'] if 'skipped' in sem else 'recorded'}")
        j = snapshot["judged"]["routing"]
        if "skipped" in j:
            print(f"judged routing: skipped: {j['skipped']}")
        else:
            print(f"judged routing: {j['passed']}/{j['cases']} ({j['score']:.0%}, {j['model']})")
        print(f"snapshot: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
