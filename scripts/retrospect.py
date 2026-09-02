#!/usr/bin/env python3
"""retrospect.py: Surface something old from the vault, on purpose and at random.

Why this exists: the vault accumulates faster than it is revisited. Notes from
six months ago are functionally deleted, not because they are wrong but because
nothing ever puts them in front of anyone again. Search does not fix this: you
have to already suspect a thing exists to search for it.

So this samples. It is deliberately not semantic search. Relevance ranking would
return what you are already thinking about, which is the opposite of the point;
the value of a random retrospective is precisely that it is not responsive to
today's context. What ranking there is only biases *away* from the recent, so
the draw skews toward material old enough to have been forgotten.

Stdlib-only, because it runs inside the routine sandbox where the semantic index
(torch, lancedb) is unavailable and `uv` cannot write its cache.

Repetition is the failure mode that would kill this fastest: the same three
notes every morning teaches the reader to skip the section. A small state file
records what has been drawn and excludes it for a cooldown window.

Usage:
    uv run scripts/retrospect.py --json
    uv run scripts/retrospect.py --count 2 --json --out picks.json
    uv run scripts/retrospect.py --no-record        # do not consume a draw

Exit codes: 0 always, including an empty draw. An empty vault is a reportable
state, not a failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import PathsError, atomic_write, fmt, vault_root  # noqa: E402

STATE_RELPATH = "_meta/retrospect_state.json"
VERDICTS_RELPATH = "_meta/retrospect_verdicts.json"

# Tiers to draw from, with weights. Reflections and wiki are the validated end
# of the corpus and are worth resurfacing more often than a raw daily note,
# which is usually a log rather than a thought.
CORPUS = {
    "reflections": 4,
    "wiki": 3,
    "research": 2,
    "daily-notes": 1,
}

# Nothing newer than this is a "retrospective"; it is just recent work.
MIN_AGE_DAYS = 60

# How long a drawn note stays out of the pool.
COOLDOWN_DAYS = 120

EXCERPT_CHARS = 700
MAX_STATE_ENTRIES = 400

_DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_ANY_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)


def content_hash(path: Path) -> str:
    """Short digest of the file's bytes.

    The verdict cache is keyed on this, not on the path alone. Without it an
    edit silently inherits the old ruling in both directions: newly added
    sensitive text would ride an old approval into an email, and a note that was
    since redacted would stay buried forever. A changed file is simply a new
    question.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def load_verdicts(ov: Path) -> dict[str, dict[str, str]]:
    """Cached review rulings, keyed by vault-relative path.

    Each entry holds the hash the ruling was made against, so a stale ruling can
    be detected rather than trusted.
    """
    path = ov / VERDICTS_RELPATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Fail closed. An unreadable cache means no note can be shown to have
        # passed review, and the draw leaves the machine.
        return {"*": {"unreadable": "1"}}
    if not isinstance(data, dict):
        return {"*": {"unreadable": "1"}}
    return {
        str(k): v
        for k, v in data.items()
        if isinstance(v, dict) and isinstance(v.get("verdict"), str)
    }


def save_verdicts(ov: Path, verdicts: dict[str, dict[str, str]]) -> None:
    atomic_write(
        ov / VERDICTS_RELPATH,
        json.dumps(verdicts, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )


def verdict_for(
    verdicts: dict[str, dict[str, str]], relative: str, digest: str
) -> str:
    """"approve", "reject", or "" when this exact content was never ruled on."""
    if "*" in verdicts:
        return "reject"
    entry = verdicts.get(relative)
    if not entry or entry.get("hash") != digest:
        return ""
    return str(entry.get("verdict") or "")


def load_state(ov: Path) -> dict[str, str]:
    path = ov / STATE_RELPATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def save_state(ov: Path, state: dict[str, str]) -> None:
    """Persist draws, keeping the file bounded.

    Oldest entries fall off first: once a note is past its cooldown the record
    of it has no further use, and an unbounded file in a synced vault is a slow
    leak nobody notices.
    """
    trimmed = dict(sorted(state.items(), key=lambda kv: kv[1], reverse=True)[:MAX_STATE_ENTRIES])
    atomic_write(
        ov / STATE_RELPATH,
        json.dumps(trimmed, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )


def note_age_days(path: Path, today: date) -> int:
    """Age from the filename date when present, else mtime.

    Same rule the digest uses: a re-synced vault rewrites mtimes wholesale, so a
    date in the name is the more trustworthy signal when one exists.
    """
    match = _DATE_IN_NAME.search(path.name)
    if match:
        try:
            when = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return (today - when).days
        except ValueError:
            pass
    try:
        return (today - date.fromtimestamp(path.stat().st_mtime)).days
    except OSError:
        return 0


def candidates(ov: Path, today: date) -> list[tuple[Path, str, int]]:
    """Every eligible note as (path, tier, weight)."""
    out: list[tuple[Path, str, int]] = []
    for tier, weight in CORPUS.items():
        root = ov / tier
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if path.name.startswith("."):
                continue
            if note_age_days(path, today) < MIN_AGE_DAYS:
                continue
            out.append((path, tier, weight))
    return out


def title_of(text: str, path: Path) -> str:
    h1 = _H1.search(text)
    if h1:
        return h1.group(1).strip()
    heading = _ANY_HEADING.search(text)
    if heading:
        return heading.group(1).strip()
    return path.stem


def excerpt_of(text: str, limit: int = EXCERPT_CHARS) -> str:
    """Prose only, bounded, cut on a sentence boundary where one is near."""
    body = _FRONTMATTER.sub("", text)
    lines: list[str] = []
    in_fence = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        if line.startswith(("|", "#", "---", "===", ">")):
            continue
        lines.append(re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line))
        if sum(len(part) for part in lines) > limit * 2:
            break
    text_out = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if len(text_out) <= limit:
        return text_out
    cut = text_out[:limit]
    boundary = max(cut.rfind("。"), cut.rfind(". "), cut.rfind("；"))
    if boundary > limit * 0.5:
        cut = cut[: boundary + 1]
    return cut.rstrip() + " …"


def draw(
    ov: Path,
    *,
    count: int = 1,
    today: date | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Pick `count` old notes, excluding anything drawn inside the cooldown."""
    today = today or date.today()
    state = load_state(ov)
    cutoff = (today - timedelta(days=COOLDOWN_DAYS)).isoformat()
    pool = [
        (path, tier, weight)
        for path, tier, weight in candidates(ov, today)
        if state.get(_key(ov, path), "") < cutoff
    ]
    if not pool:
        return []

    verdicts = load_verdicts(ov)
    rng = random.Random(seed)
    picks: list[dict[str, Any]] = []
    weights = [weight for _, _, weight in pool]
    # Over-draw. A rejected note costs a draw but yields nothing, so stopping at
    # `count` candidates would routinely return an empty section once the cache
    # has any rejections in it.
    attempts = 0
    limit = min(len(pool), count * 8 + 8)
    while pool and len(picks) < count and attempts < limit:
        attempts += 1
        index = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        path, tier, _weight = pool.pop(index)
        weights.pop(index)
        digest = content_hash(path)
        if not digest:
            continue
        ruling = verdict_for(verdicts, _key(ov, path), digest)
        if ruling == "reject":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        picks.append(
            {
                "path": _key(ov, path),
                "tier": tier,
                "title": title_of(text, path),
                "age_days": note_age_days(path, today),
                "excerpt": excerpt_of(text),
                "content_hash": digest,
                # Fail closed: an unreviewed pick is a question, not an answer.
                # The caller must not surface it until a reviewer has ruled.
                "reviewed": ruling == "approve",
            }
        )
    return picks


def _key(ov: Path, path: Path) -> str:
    try:
        return str(path.relative_to(ov))
    except ValueError:
        return str(path)


def record(ov: Path, picks: list[dict[str, Any]], today: date | None = None) -> None:
    today = today or date.today()
    state = load_state(ov)
    for pick in picks:
        state[str(pick["path"])] = today.isoformat()
    save_state(ov, state)


def apply_verdicts(ov: Path, path: Path) -> int:
    """Record reviewer rulings so the same note is never re-reviewed for free."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"verdicts unreadable: {exc!r}", file=sys.stderr)
        return 1
    if isinstance(rows, dict):
        rows = rows.get("verdicts") or []
    if not isinstance(rows, list):
        print("verdicts must be a list", file=sys.stderr)
        return 1

    stored = load_verdicts(ov)
    stored.pop("*", None)
    applied = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        relative = str(row.get("path") or "")
        digest = str(row.get("content_hash") or "")
        ruling = str(row.get("verdict") or "")
        if not relative or not digest or ruling not in {"approve", "reject"}:
            continue
        entry = {
            "hash": digest,
            "verdict": ruling,
            "reviewed": date.today().isoformat(),
        }
        # The reviewer's reason is kept so a ruling can be audited later without
        # re-reading the note. It is a category, never a quotation: reproducing
        # what made a note sensitive inside the cache would defeat the rejection.
        reason = str(row.get("reason") or "").strip()
        if reason:
            entry["reason"] = reason[:200]
        stored[relative] = entry
        applied += 1
    if applied:
        save_verdicts(ov, stored)
    print(f"recorded {applied} verdict(s)")
    return 0


def text_report(picks: list[dict[str, Any]]) -> str:
    if not picks:
        return "nothing eligible to resurface (empty pool or everything inside cooldown)"
    lines = []
    for pick in picks:
        years = pick["age_days"] / 365
        age = f"{years:.1f}y" if years >= 1 else f"{pick['age_days']}d"
        state = "reviewed" if pick.get("reviewed") else "NEEDS REVIEW"
        lines.append(f"[{pick['tier']} · {age} · {state}] {pick['title']}")
        lines.append(f"  {pick['excerpt'][:200]}")
        lines.append(f"  {pick['path']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resurface an old note at random.")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="Write output to a file instead of stdout.")
    parser.add_argument("--today", help="Override today (YYYY-MM-DD).")
    parser.add_argument("--seed", type=int, help="Deterministic draw, for tests.")
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="Do not mark the draw as used; the same notes stay eligible.",
    )
    parser.add_argument(
        "--apply-verdicts",
        help=(
            "JSON file of reviewer rulings: "
            '[{"path": ..., "content_hash": ..., "verdict": "approve"|"reject"}]. '
            "Cached against the hash, so an edited note is asked about again."
        ),
    )
    args = parser.parse_args(argv)

    try:
        ov = vault_root()
    except PathsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    today = date.today()
    if args.today:
        try:
            today = datetime.strptime(args.today, "%Y-%m-%d").date()
        except ValueError:
            print(f"--today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
            return 1

    if args.apply_verdicts:
        return apply_verdicts(ov, Path(args.apply_verdicts))

    picks = draw(ov, count=max(1, args.count), today=today, seed=args.seed)
    if picks and not args.no_record:
        record(ov, picks, today)

    payload = (
        json.dumps({"schema": 1, "picks": picks}, indent=2, ensure_ascii=False)
        if args.json
        else text_report(picks)
    )
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {fmt(Path(args.out))} ({len(picks)} pick(s))")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
