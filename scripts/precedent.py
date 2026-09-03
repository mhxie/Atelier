#!/usr/bin/env python3
"""Precedent judge: the default a person would have chosen, from their ledger.

Reads `scripts/decisions.py`'s ledger, finds the past human decisions most
like a new item (deterministic pre-filter), asks a cheap model to apply
them, and gates the answer before anything becomes a default:

  the verdict names an executable action, confidence >= --min-confidence
  (0.8), at least --min-precedents (3) DISTINCT cited precedents that each
  score >= --min-similarity (2.0) and all agree with the verdict, the class's
  measured precedent accuracy >= --min-accuracy (0.9) once --min-judged (5)
  defaults have been judged, and fewer than --max-unconfirmed (10) defaults
  set in this class since the user last decided anything. Anything else stays
  a human decision.

The last condition is the silent budget, and it is the brake that a user who
stops looking can still reach: accuracy scores an unchallenged default as
correct, so it loosens as engagement falls, while the budget tightens. Any
human decision in any class refills it. `--max-unconfirmed 0` makes the judge
a sorter: it ranks and explains, it never decides alone.

Subcommands (each prints one JSON object):
  bundle   pre-filter: nearest precedents + the judge prompt for one item
  judge    gate a model judgment (from --judgment FILE or a direct --model call)
  autoevo  end to end over pending queue entries without a default:
           bundle, judge, and `autoevo_pending.py set-default` on a pass

Who judges is an explicit choice, never a fallback: `--judgment FILE` /
`--judgment-dir DIR` take verdicts a native subagent (the `precedent-judge`
role) wrote after reading the prompts `--bundle-dir` emits, and `--model` or
`ATELIER_PRECEDENT_MODEL` names a direct-API identity for
`scripts/chat_completion.py`. Without one of those nothing is judged: the
bundle carries note paths, evidence, and past reasons, and they must not
leave the machine by default.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decisions  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Only these verdicts name an action the runner can execute. Anything else
# (`undo`, `clarified:*`, a typo) stays a human decision instead of falling
# through to the apply branch.
EXECUTABLE_VERDICTS = ("apply", "dismiss")
JUDGE_MAX_TOKENS = 600
STOPWORDS = {"the", "a", "an", "of", "to", "in", "and", "or", "for", "on", "is", "it", "this", "that", "with", "by", "at"}
JUDGE_SYSTEM = (
    "You apply one person's own past decisions to a new item of the same kind. "
    "You never invent policy: if the precedents are mixed, thin, or do not fit, answer verdict \"human\". "
    "Reply with one JSON object and nothing else."
)


def tokens(text: Any) -> set[str]:
    return {t for t in re.findall(r"\w+", str(text or "").casefold()) if len(t) > 1 and t not in STOPWORDS}


def _feature_text(features: dict[str, Any]) -> str:
    return " ".join(str(features.get(k, "")) for k in ("proposed_action", "evidence_summary", "candidates", "input"))


def similarity(item: dict[str, Any], row: dict[str, Any], today: date) -> float:
    a, b = item.get("features", {}), row.get("features", {})
    score = 0.0
    if a.get("tier") and a.get("tier") == b.get("tier"):
        score += 2.0
    ta, tb = tokens(_feature_text(a)), tokens(_feature_text(b))
    if ta and tb:
        score += 3.0 * len(ta & tb) / len(ta | tb)
    try:
        if date.fromisoformat(str(row.get("ts", ""))[:10]) >= today - timedelta(days=180):
            score += 1.0
    except ValueError:
        pass
    return round(score, 3)


def nearest_precedents(rows: list[dict[str, Any]], cls: str, item: dict[str, Any], today: date, k: int) -> list[dict[str, Any]]:
    # Tier partitions, it does not merely score. The ledger's verdicts hinge on
    # tier (in autoevo/time-stale-A every reflections-tier decision is dismiss and
    # every wip-tier one is apply), while token Jaccard mostly measures how
    # formulaic a category's summaries are: its median runs 0.09 in time-stale-A
    # and 0.77 in low-signal, so one cross-class threshold cannot mean
    # "related". Folding tier into the score let overlap buy back a tier
    # mismatch; filtering means a cross-tier row is never shown to the judge and
    # never citable. Items with no resolvable tier fall back to ranking alone.
    item_tier = str(item.get("features", {}).get("tier") or "")
    scored = []
    for row in rows:
        if row.get("class") != cls or row.get("by") != "human" or row.get("verdict") == "defer":
            continue
        if item_tier and str(row.get("features", {}).get("tier") or "") != item_tier:
            continue
        scored.append((similarity(item, row, today), str(row.get("ts", "")), row))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for score, _, row in scored[:k]:
        out.append(
            {
                "ts": row.get("ts"),
                "verdict": row.get("verdict"),
                "reason": row.get("reason"),
                "similarity": score,
                "features": {k2: row.get("features", {}).get(k2) for k2 in ("tier", "proposed_action", "evidence_summary", "candidates") if row.get("features", {}).get(k2) is not None},
            }
        )
    return out


def render_prompt(cls: str, item: dict[str, Any], precedents: list[dict[str, Any]]) -> str:
    verdicts = sorted({str(p["verdict"]) for p in precedents}) or ["apply", "dismiss"]
    lines = [
        f"Decision class: {cls}",
        "New item:",
        json.dumps(item, ensure_ascii=False, indent=2),
        "",
        f"Past decisions by the same person, most similar first ({len(precedents)}):",
    ]
    for i, p in enumerate(precedents):
        lines.append(f"[{i}] {p['ts'][:10]} verdict={p['verdict']} | reason: {p['reason']} | {json.dumps(p['features'], ensure_ascii=False)}")
    lines += [
        "",
        "Answer with JSON: {\"verdict\": <one of " + ", ".join(verdicts) + ", human>, "
        "\"confidence\": <0..1>, \"cited\": [<indices of the precedents you relied on>], "
        "\"reason\": <one sentence that names the precedents and the feature that made them apply>}",
        "Use \"human\" when the cited precedents disagree with each other, when fewer than three fit, "
        "or when the new item differs from them on the feature the reasons hinge on.",
    ]
    return "\n".join(lines)


def build_bundle(*, cls: str, subject: str, features: dict[str, Any], ledger: Path | None, today: date, k: int) -> dict[str, Any]:
    rows = decisions.load(ledger)
    item = {"subject": subject, "features": features}
    precedents = nearest_precedents(rows, cls, item, today, k)
    stats = dict(decisions.precedent_stats(rows, today, cls).get(cls, {}))
    stats["precedent_unconfirmed_streak"] = decisions.unconfirmed_since_heartbeat(rows, cls)
    return {"class": cls, "item": item, "precedents": precedents, "stats": stats, "prompt": render_prompt(cls, item, precedents)}


def parse_judgment(text: str) -> dict[str, Any]:
    raw = text.strip()
    fence = re.search(r"\{.*\}", raw, re.DOTALL)
    if fence:
        raw = fence.group(0)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("judgment must be a JSON object")
    return data


def _similarity_at_least(value: Any, floor: float) -> bool:
    """True when a cited precedent's similarity is a real number >= floor.

    `similarity()` returns 0.0 for a row that shares no tier, no tokens, and no
    recency; such a row is still returned by the pre-filter and was still a
    citable index, so the floor is what makes "cited" mean "related".
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    value = float(value)
    return math.isfinite(value) and value >= floor


def gate(bundle: dict[str, Any], judgment: dict[str, Any], *, min_confidence: float, min_precedents: int, min_accuracy: float, min_judged: int, min_similarity: float = 2.0, max_unconfirmed: int = 10) -> dict[str, Any]:
    verdict = str(judgment.get("verdict", "human")).strip()
    try:
        confidence = float(judgment.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):
        # NaN compares False against every threshold, so it would pass the
        # confidence gate rather than fail it.
        confidence = 0.0
    # `type(i) is int` rejects bool (a model answering `cited` as a mask sends
    # [true, true, true], and isinstance(True, int) is True); the set drops
    # duplicates, so [0, 0, 0] can no longer satisfy min_precedents with one row.
    cited_idx = sorted({i for i in judgment.get("cited", []) if type(i) is int and 0 <= i < len(bundle["precedents"])})
    cited = [bundle["precedents"][i] for i in cited_idx]
    weak = [p for p in cited if not _similarity_at_least(p.get("similarity"), min_similarity)]
    result = {
        "verdict": verdict,
        "confidence": confidence,
        "cited": cited_idx,
        "reason": str(judgment.get("reason", "")).strip(),
        "default": False,
        "gate": "",
    }
    stats = bundle.get("stats") or {}
    judged = int(stats.get("precedent_judged") or 0)
    accuracy = stats.get("precedent_accuracy")
    unconfirmed = int(stats.get("precedent_unconfirmed_streak") or 0)
    if verdict == "human" or not verdict:
        result["gate"] = "judge returned human"
    elif verdict not in EXECUTABLE_VERDICTS:
        result["gate"] = f"verdict {verdict!r} is not an executable default"
    elif confidence < min_confidence:
        result["gate"] = f"confidence {confidence:.2f} < {min_confidence}"
    elif len(cited) < min_precedents:
        result["gate"] = f"{len(cited)} distinct cited precedents < {min_precedents}"
    elif any(str(p.get("verdict")) != verdict for p in cited):
        result["gate"] = "cited precedents disagree with the verdict"
    elif weak:
        result["gate"] = f"{len(weak)} cited precedent(s) below similarity {min_similarity}"
    elif judged >= min_judged and isinstance(accuracy, (int, float)) and accuracy < min_accuracy:
        result["gate"] = f"class precedent accuracy {accuracy} < {min_accuracy} over {judged} judged"
    elif unconfirmed >= max_unconfirmed:
        # The silent budget, and the only brake that a user who never looks can
        # actually reach: accuracy counts an unchallenged default as correct, so
        # it loosens as engagement falls. This tightens instead, and any human
        # decision anywhere resets it. max_unconfirmed = 0 makes the judge a
        # sorter: it still ranks and explains, it never decides alone.
        result["gate"] = (
            f"{unconfirmed} unconfirmed default(s) in this class since the last human decision "
            f">= {max_unconfirmed}; run /autoevo-review"
        )
    else:
        result["default"] = True
        result["gate"] = "pass"
    return result


def resolve_model(explicit: str | None) -> str | None:
    """Only an explicit identity; there is no hosted-model fallback."""
    return explicit or os.environ.get("ATELIER_PRECEDENT_MODEL") or None


def call_model(model: str, prompt: str, timeout: int = 180) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
        handle.write(prompt)
        prompt_path = handle.name
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "chat_completion.py"), "--model", model, "--system", JUDGE_SYSTEM,
             "--prompt-file", prompt_path, "--task-type", "precedent-judge", "--max-tokens", str(JUDGE_MAX_TOKENS)],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        )
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError(f"chat_completion exit {proc.returncode}: {proc.stderr.strip()[:200]}")
    return parse_judgment(proc.stdout)


def _gate_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "min_confidence": args.min_confidence,
        "min_precedents": args.min_precedents,
        "min_accuracy": args.min_accuracy,
        "min_judged": args.min_judged,
        "min_similarity": args.min_similarity,
        "max_unconfirmed": args.max_unconfirmed,
    }


def cmd_bundle(args: argparse.Namespace) -> int:
    features = json.loads(args.features_json) if args.features_json else {}
    bundle = build_bundle(
        cls=args.cls, subject=args.subject, features=features,
        ledger=Path(args.ledger) if args.ledger else None,
        today=date.fromisoformat(args.today) if args.today else date.today(), k=args.k,
    )
    print(json.dumps(bundle, ensure_ascii=False, indent=2))
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    if args.judgment:
        judgment = parse_judgment(Path(args.judgment).read_text(encoding="utf-8"))
        model = "supplied"
    else:
        model = resolve_model(args.model)
        if not model:
            print(json.dumps({"error": "no judge available: pass --judgment FILE or bind a direct-API model", "default": False}))
            return 1
        try:
            judgment = call_model(model, bundle["prompt"])
        except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            print(json.dumps({"error": f"judge failed: {exc}", "default": False}))
            return 1
    result = gate(bundle, judgment, **_gate_kwargs(args))
    result["model"] = model
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _queue_entries(queue: Path) -> list[dict[str, Any]]:
    if not queue.is_file():
        return []
    return tomllib.loads(queue.read_text(encoding="utf-8")).get("pending", [])


def cmd_autoevo(args: argparse.Namespace) -> int:
    """Judge every pending queue entry that has no default yet."""
    import autoevo_pending

    queue = Path(args.queue) if args.queue else autoevo_pending.queue_path()
    ledger = Path(args.ledger) if args.ledger else None
    today = date.fromisoformat(args.today) if args.today else date.today()
    model = None if (args.judgment_dir or args.bundle_dir) else resolve_model(args.model)
    if not args.judgment_dir and not args.bundle_dir and not model:
        print(json.dumps({"error": "no judge chosen: pass --bundle-dir (native judge), --judgment-dir, or --model", "judged": [], "defaults_set": []}))
        return 2
    judged, defaults, bundles = [], [], []
    for entry in _queue_entries(queue):
        if entry.get("status") != "pending" or entry.get("default_action"):
            continue
        cls = f"autoevo/{entry.get('category')}"
        bundle = build_bundle(cls=cls, subject=str(entry.get("id")), features=decisions.autoevo_features(entry), ledger=ledger, today=today, k=args.k)
        if len(bundle["precedents"]) < args.min_precedents:
            judged.append({"id": entry.get("id"), "default": False, "gate": f"only {len(bundle['precedents'])} precedents"})
            continue
        if args.bundle_dir:
            # Emit the prompt for a native judge; nothing leaves the machine.
            out_dir = Path(args.bundle_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{entry.get('id')}.bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
            (out_dir / f"{entry.get('id')}.prompt.txt").write_text(JUDGE_SYSTEM + "\n\n" + bundle["prompt"] + "\n", encoding="utf-8")
            bundles.append(entry.get("id"))
            judged.append({"id": entry.get("id"), "default": False, "gate": "bundled for native judge"})
            continue
        try:
            if args.judgment_dir:
                judgment_path = Path(args.judgment_dir) / f"{entry.get('id')}.json"
                if not judgment_path.is_file():
                    judged.append({"id": entry.get("id"), "default": False, "gate": "no judgment file"})
                    continue
                judgment = parse_judgment(judgment_path.read_text(encoding="utf-8"))
            else:
                judgment = call_model(str(model), bundle["prompt"])
        except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            judged.append({"id": entry.get("id"), "default": False, "gate": f"judge failed: {exc}"})
            continue
        result = gate(bundle, judgment, **_gate_kwargs(args))
        row = {"id": entry.get("id"), "verdict": result["verdict"], "confidence": result["confidence"], "default": result["default"], "gate": result["gate"]}
        if result["default"]:
            action = "dismiss" if result["verdict"] == "dismiss" else ("stale-banner" if autoevo_pending.default_for(entry) == "stale-banner" else None)
            if action is None:
                row.update({"default": False, "gate": f"verdict {result['verdict']} has no executable default for this entry"})
            elif args.dry_run:
                row["would_set"] = action
            else:
                reason = f"precedent ({len(result['cited'])} cited): {result['reason']}"
                cmd = [sys.executable, str(ROOT / "scripts" / "autoevo_pending.py")]
                if args.queue:
                    cmd += ["--queue", str(queue)]
                if ledger:
                    cmd += ["--ledger", str(ledger)]
                cmd += ["set-default", "--id", str(entry.get("id")), "--action", action, "--today", today.isoformat(),
                        "--reason", reason, "--by", "precedent", "--source", "nightly"]
                proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
                try:
                    payload = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    payload = {"error": proc.stderr.strip()[:200]}
                if "error" in payload:
                    row.update({"default": False, "gate": f"set-default failed: {payload['error']}"})
                else:
                    row.update({"set": action, "default_at": payload.get("default_at")})
                    defaults.append(entry.get("id"))
        judged.append(row)
    print(json.dumps({
        "queue": str(queue), "today": today.isoformat(),
        "model": model or ("native-judge" if args.bundle_dir else "judgment-dir"),
        "judged": judged, "defaults_set": defaults, "bundled": bundles, "dry_run": bool(args.dry_run),
    }, ensure_ascii=False, sort_keys=True))
    return 0


def _add_gate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--min-confidence", type=float, default=0.8)
    p.add_argument("--min-precedents", type=int, default=3)
    p.add_argument("--min-accuracy", type=float, default=0.9)
    p.add_argument("--min-judged", type=int, default=5)
    # 2.0 is the tier term in similarity() (+2.0 same tier, +3.0 * token Jaccard,
    # +1.0 within 180 days), so a cited row below it does not share the item's
    # tier and is not a precedent. `nearest_precedents` already excludes those;
    # this is the gate's own check, for callers that build a bundle another way.
    p.add_argument("--min-similarity", type=float, default=2.0)
    # Silent budget: how many defaults this class may set without the user
    # deciding anything, anywhere. 0 makes the judge a sorter.
    p.add_argument("--max-unconfirmed", type=int, default=10)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", default=None, help="Override the decision ledger path (tests).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bundle")
    p.add_argument("--class", dest="cls", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--features-json", default=None)
    p.add_argument("--today", default=None)
    p.add_argument("--k", type=int, default=20)
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("judge")
    p.add_argument("--bundle", required=True)
    p.add_argument("--judgment", default=None, help="JSON from a native subagent that read the bundle prompt.")
    p.add_argument("--model", default=None, help="Direct-API model identity (explicit opt-in; or ATELIER_PRECEDENT_MODEL).")
    _add_gate_args(p)
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser("autoevo")
    p.add_argument("--queue", default=None)
    p.add_argument("--today", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--judgment-dir", default=None, help="Directory of <id>.json judgments written by the native judge (or a test).")
    p.add_argument("--bundle-dir", default=None, help="Write <id>.prompt.txt / <id>.bundle.json for the native judge instead of judging.")
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--dry-run", action="store_true")
    _add_gate_args(p)
    p.set_defaults(func=cmd_autoevo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
