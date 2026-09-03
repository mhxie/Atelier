#!/usr/bin/env python3
"""Deterministic Forgetter bands: low-signal fully, redundant via retrieval.

The nightly sweep dispatched a 60-turn model agent to evaluate conditions a
script can check (`.claude/agents/forgetter.md` § Low-signal: five
conjunctive conditions; § Redundant: top-5 retrieval overlap). This scanner
computes those two bands; era-stale and contradicted remain model judgment.

Low-signal (ALL five, per the agent spec):
  words < 150; zero inbound wikilinks; zero #tags; mtime older than 90 days;
  path under <paths.wip>/.

Redundant (with --redundant; needs the semantic index):
  3+ peers in the candidate's top-5 retrieval above the floor (real-mode
  default 0.6), self-matches dropped, and only working-tier peers count
  (papers/preprints/wiki/profile/daily-notes are the note's subject, not its
  duplicate).

Output: one JSON object with per-band findings carrying the same evidence
fields the Forgetter envelope records, so the nightly command (or an
interactive /hi forget) can consume either source interchangeably.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier, tier_segments, vault_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKING_TIERS = ("wip", "research", "reflections", "agent_findings")
SUBJECT_PREFIX_TIERS = ("papers", "preprints", "wiki", "daily_notes")
LOW_SIGNAL_MAX_WORDS = 150
LOW_SIGNAL_MIN_AGE_DAYS = 90
REDUNDANT_FLOOR = 0.6
REDUNDANT_MIN_PEERS = 3
TAG_RE = re.compile(r"#[A-Za-z]")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")


def _inbound_link_index(vault: Path) -> set[str]:
    """One pass over the vault: the set of wikilinked titles (casefolded)."""
    titles: set[str] = set()
    for path in vault.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in WIKILINK_RE.finditer(text):
            titles.add(m.group(1).strip().casefold())
    return titles


def low_signal_content_failures(vault: Path, rel: str) -> list[str]:
    """Which non-age low-signal conditions do NOT hold for one note.

    Empty means every one of them holds. The five conditions are conjunctive on
    purpose: each alone catches a deliberate stub, a brand-new note, or an
    intentional archive, so four-of-five is a working note, not a flag. Age is
    the caller's to supply, because callers differ on the clock they trust.

    Exists so a consumer can RECOMPUTE the rule from disk instead of trusting a
    `conditions_met` count it was handed.
    """
    failures: list[str] = []
    wip_prefix = f"{tier('wip').relative_to(vault)}/"
    if not str(rel).startswith(wip_prefix):
        failures.append(f"not under {wip_prefix}")
    path = vault / rel
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return [*failures, "unreadable on disk"]
    words = len(text.split())
    if words >= LOW_SIGNAL_MAX_WORDS:
        failures.append(f"{words} words >= {LOW_SIGNAL_MAX_WORDS}")
    if TAG_RE.search(text):
        failures.append("carries #tags")
    if path.stem.casefold() in _inbound_link_index(vault):
        failures.append("has inbound wikilinks")
    return failures


def scan_low_signal(vault: Path, now: float) -> list[dict]:
    wip = tier("wip")
    if not wip.is_dir():
        return []
    linked = _inbound_link_index(vault)
    findings = []
    for path in sorted(wip.rglob("*.md")):
        try:
            stat = path.stat()
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        age_days = int((now - stat.st_mtime) / 86400)
        if age_days <= LOW_SIGNAL_MIN_AGE_DAYS:
            continue
        words = len(text.split())
        if words >= LOW_SIGNAL_MAX_WORDS:
            continue
        if TAG_RE.search(text):
            continue
        if path.stem.casefold() in linked:
            continue
        findings.append(
            {
                "band": "low-signal",
                "path": str(path.relative_to(vault)),
                "words": words,
                "links_in": 0,
                "tags": 0,
                "age_days": age_days,
            }
        )
    return findings


def _tier_of(rel_path: str) -> str | None:
    segments = tier_segments()
    for name in (*WORKING_TIERS, *SUBJECT_PREFIX_TIERS):
        seg = segments.get(name)
        if seg and (rel_path == seg or rel_path.startswith(seg.rstrip("/") + "/")):
            return name
    return None


def scan_redundant(vault: Path, scope: str, max_candidates: int, floor: float) -> list[dict]:
    base = tier(scope)
    if not base.is_dir():
        return []
    candidates = sorted(base.rglob("*.md"))[:max_candidates]
    findings = []
    for path in candidates:
        title = path.stem
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "semantic.py"), "query", title,
             "--top", "5", "--format", "json", "--sources", "local"],
            cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode != 0:
            continue
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        rel_self = str(path.relative_to(vault))
        peers = []
        for row in rows if isinstance(rows, list) else []:
            rel = str(row.get("path", ""))
            score = row.get("score")
            if rel == rel_self or not isinstance(score, (int, float)) or score < floor:
                continue
            if _tier_of(rel) not in WORKING_TIERS:
                continue  # subject documents never count as duplicates
            peers.append({"path": rel, "score": score})
        if len(peers) >= REDUNDANT_MIN_PEERS:
            findings.append(
                {"band": "redundant", "path": rel_self, "peers": peers, "floor": floor}
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redundant", action="store_true", help="Also run the retrieval-overlap band (needs the semantic index).")
    parser.add_argument("--scope", default="wip", help="Tier name for the redundant band (default wip).")
    parser.add_argument("--max-candidates", type=int, default=15)
    parser.add_argument("--floor", type=float, default=REDUNDANT_FLOOR)
    args = parser.parse_args(argv)

    vault = vault_root()
    now = time.time()
    payload = {
        "low_signal": scan_low_signal(vault, now),
        "redundant": scan_redundant(vault, args.scope, args.max_candidates, args.floor) if args.redundant else [],
        "redundant_ran": bool(args.redundant),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
