#!/usr/bin/env python3
"""Evaluate QMD against explicit {query, target} pairs; never manufacture a gold set."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semantic  # noqa: E402

GOLD_PATH = Path(__file__).with_name("_evalset.json")


def evaluate(gold: list[dict], args: argparse.Namespace) -> dict:
    ranks = []
    started = time.monotonic()
    for item in gold:
        if not isinstance(item, dict) or not isinstance(item.get("query"), str) or not item["query"].strip():
            raise ValueError("each gold row needs a nonempty query")
        target = item.get("target")
        if not isinstance(target, str) or not target or target.startswith("/") or ".." in Path(target).parts:
            raise ValueError("each gold row needs a safe vault-relative target")
        flags = ["query", item["query"], "--top", "10", "--scope", args.scope, "--mode", args.mode]
        if args.no_rerank:
            flags.append("--no-rerank")
        rows = semantic.query(semantic.build_parser().parse_args(flags))
        paths = [row["path"] for row in rows]
        ranks.append(paths.index(target) + 1 if target in paths else None)
    elapsed = time.monotonic() - started
    size = len(ranks)
    if not size:
        raise ValueError("gold set is empty")
    return {
        "config": {"backend": "qmd", "mode": args.mode, "scope": args.scope,
                   "rerank": args.mode == "hybrid" and not args.no_rerank},
        "n_queries": size,
        "recall@5": sum(rank is not None and rank <= 5 for rank in ranks) / size,
        "recall@10": sum(rank is not None for rank in ranks) / size,
        "MRR@10": sum(1 / rank for rank in ranks if rank is not None) / size,
        "nDCG@10": sum(1 / math.log2(rank + 1) for rank in ranks if rank is not None) / size,
        "elapsed_s": round(elapsed, 3),
        "qps": round(size / max(elapsed, .001), 3),
        "ranks": ranks,
    }


def cmd_run(args: argparse.Namespace) -> int:
    try:
        gold = json.loads((args.gold or GOLD_PATH).read_text(encoding="utf-8"))
        if not isinstance(gold, list) or args.limit < 0:
            raise ValueError("gold must be a list and --limit must be nonnegative")
        if args.limit:
            gold = gold[:args.limit]
        print(json.dumps(evaluate(gold, args), ensure_ascii=False))
        return 0
    except (semantic.SearchError, OSError, ValueError) as exc:
        print(f"eval aborted: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--gold", type=Path)
    run.add_argument("--scope", choices=semantic.SCOPES + ("all",), default="active")
    run.add_argument("--mode", choices=("hybrid", "lexical", "vector"), default="hybrid")
    run.add_argument("--no-rerank", action="store_true")
    run.add_argument("--limit", type=int, default=0)
    run.set_defaults(func=cmd_run)
    return parser


if __name__ == "__main__":
    arguments = build_parser().parse_args()
    raise SystemExit(arguments.func(arguments))
