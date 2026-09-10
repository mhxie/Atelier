#!/usr/bin/env python3
"""Convert Obsidian wikilinks to portable Markdown.

Notes and full dates resolve to relative links; numeric targets become text and
unresolved names become semantic tags. Image embeds stay images, while note
transclusions degrade to links. Code regions are masked during conversion.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from _paths import atomic_write, vault_root  # type: ignore[import-not-found]  # noqa: E402

OV = vault_root()

# Mirrors zk/.gitignore semantics: dirs that never contain Reflect-bound or
# GitHub-bound markdown.
SKIP_DIRS = {"secure", "personal", "cache", ".obsidian", ".trash", "raw"}

IMAGE_EXTS = "png|jpg|jpeg|gif|svg|webp"

WIKILINK_RE = re.compile(r"\[\[([^\]]*)\]\]")
EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
IMAGE_EXT_RE = re.compile(rf"\.(?:{IMAGE_EXTS})$", re.IGNORECASE)

# Date pattern detectors (try in order)
ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
US_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
ZH_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")
REFLECT_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,\s+(\d{4})$",
    re.IGNORECASE,
)
ENGLISH_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,\s+(\d{4})$",
    re.IGNORECASE,
)
PURE_DIGITS_RE = re.compile(r"^\d+$")

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_date(s: str) -> Optional[date]:
    """Parse legacy date strings to a date object. Returns None if unparseable."""
    s = s.strip()
    for regex, order in (
        (ISO_RE, ("Y", "M", "D")),
        (US_SLASH_RE, ("M", "D", "Y")),
        (ZH_RE, ("Y", "M", "D")),
    ):
        m = regex.match(s)
        if m:
            try:
                vals = dict(zip(order, m.groups()))
                return date(int(vals["Y"]), int(vals["M"]), int(vals["D"]))
            except (ValueError, KeyError):
                return None
    for regex in (REFLECT_RE, ENGLISH_RE):
        m = regex.match(s)
        if m:
            try:
                month_name = m.group(1).lower()
                day = int(m.group(2))
                year = int(m.group(3))
                return date(year, MONTHS[month_name], day)
            except (ValueError, KeyError):
                return None
    return None


def slugify_tag(s: str) -> str:
    """Tag slug: lowercase, kebab-case, unicode-preserving."""
    s = s.strip().lower()
    s = re.sub(r"[\s\W]+", "-", s, flags=re.UNICODE)
    return s.strip("-")


def slugify_anchor(s: str) -> str:
    """GitHub-style heading anchor slug."""
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s)
    return s.strip("-")


def build_stem_index(zk: Path) -> dict[str, list[Path]]:
    """Map stem (lowercase) → list of paths relative to zk/."""
    idx: dict[str, list[Path]] = defaultdict(list)
    for f in zk.rglob("*.md"):
        rel = f.relative_to(zk)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        idx[f.stem.lower()].append(rel)
    return idx


def resolve_target(target: str, source_rel: Path, idx: dict[str, list[Path]]) -> Optional[Path]:
    """Resolve target stem to a path. Source-tier-aware preference."""
    target = target.strip().rstrip("/")
    stem = target.split("/")[-1]
    matches = idx.get(stem.lower(), [])
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    source_tier = source_rel.parts[0] if source_rel.parts else None
    same_tier = [m for m in matches if m.parts and m.parts[0] == source_tier]
    if same_tier:
        return same_tier[0]
    wiki = [m for m in matches if m.parts and m.parts[0] == "wiki"]
    if wiki:
        return wiki[0]
    return matches[0]


def relative_path(target_rel: Path, source_rel: Path) -> str:
    """Compute relative path from source file's dir to target file."""
    source_parts = source_rel.parts[:-1]
    target_parts = target_rel.parts
    common = 0
    for a, b in zip(source_parts, target_parts):
        if a == b:
            common += 1
        else:
            break
    ups = len(source_parts) - common
    rest = target_parts[common:]
    parts = [".."] * ups + list(rest)
    return "/".join(parts) if parts else target_rel.name


def md_link(display: str, path: str) -> str:
    """Markdown link with angle-bracket URL wrapping when path contains
    characters (space, parens, etc.) that render unreliably in plain form."""
    if any(c in path for c in " ()"):
        return f"[{display}](<{path}>)"
    return f"[{display}]({path})"


def transform_wikilink(inner: str, source_rel: Path, idx: dict[str, list[Path]]) -> str:
    """Transform a single [[...]] wikilink content. Returns full replacement (no brackets)."""
    raw = inner.strip()
    if not raw:
        return ""

    # Parse alias and anchor
    alias_parts = raw.split("|", 1)
    target_part = alias_parts[0].strip()
    alias: Optional[str] = alias_parts[1].strip() if len(alias_parts) > 1 else None

    anchor_parts = target_part.split("#", 1)
    target = anchor_parts[0].strip()
    anchor: Optional[str] = anchor_parts[1].strip() if len(anchor_parts) > 1 else None
    if anchor and anchor.startswith("^"):
        anchor = None  # block ref dropped (GitHub has no block-level anchors)

    if not target:
        return ""

    # Pure-digit (year/number) → strip
    if PURE_DIGITS_RE.match(target):
        return alias or target

    # Date detection (full target only, no anchor)
    parsed = parse_date(target)
    if parsed:
        iso = parsed.strftime("%Y-%m-%d")
        year = parsed.strftime("%Y")
        month = parsed.strftime("%m")
        daily_rel = Path("daily-notes") / year / month / f"{iso}.md"
        if (OV / daily_rel).exists():
            display = alias or iso
            return md_link(display, relative_path(daily_rel, source_rel))
        return alias or iso

    # Try resolve as note
    resolved = resolve_target(target, source_rel, idx)
    if resolved:
        display = alias or target
        rel = relative_path(resolved, source_rel)
        if anchor:
            return md_link(display, f"{rel}#{slugify_anchor(anchor)}")
        return md_link(display, rel)

    # Unresolved → tag (drop alias if any)
    tag = slugify_tag(target)
    if not tag:
        return ""
    if PURE_DIGITS_RE.match(tag):
        return alias or tag  # pure-digit slug → plain text
    return f"#{tag}"


def transform_embed(inner: str, source_rel: Path, idx: dict[str, list[Path]]) -> str:
    """Transform ![[...]]. Image extension → standard ![](), else degrade to wikilink."""
    inner = inner.strip()
    if IMAGE_EXT_RE.search(inner):
        if any(c in inner for c in " ()"):
            return f"![](<{inner}>)"
        return f"![]({inner})"
    # Non-image embed (note transclusion) → degrade to standard link
    return transform_wikilink(inner, source_rel, idx)


INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)")


def mask_code(text: str) -> tuple[str, list[str]]:
    """Replace code regions with sentinel placeholders. Returns (masked, originals)."""
    originals: list[str] = []

    def _mask(value: str) -> str:
        originals.append(value)
        return f"\x00CODE_{len(originals) - 1}\x00"

    lines = text.splitlines(keepends=True)
    for token in reversed(MarkdownIt().parse(text)):
        if token.map and token.type in {"fence", "code_block", "inline"}:
            start, end = token.map
            block = "".join(lines[start:end])
            masked = (INLINE_CODE_RE.sub(lambda m: _mask(m.group(0)), block)
                      if token.type == "inline" else _mask(block))
            lines[start:end] = [masked]
    return "".join(lines), originals


def unmask_code(text: str, originals: list[str]) -> str:
    """Restore sentinels back to original code spans."""
    for i in range(len(originals) - 1, -1, -1):
        orig = originals[i]
        text = text.replace(f"\x00CODE_{i}\x00", orig)
    return text


def transform_file(path: Path, idx: dict[str, list[Path]]) -> tuple[str, list[tuple[str, str]]]:
    """Transform a file's text. Returns (new_text, [(old, new), ...]).

    Wikilinks inside fenced or inline code are preserved verbatim — they may
    document syntax rather than reference notes."""
    with path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    source_rel = path.resolve().relative_to(OV)
    diffs: list[tuple[str, str]] = []

    # Mask code regions so wikilink regex doesn't touch them.
    masked, code_originals = mask_code(text)

    def _embed_sub(m: re.Match) -> str:
        old = m.group(0)
        new = transform_embed(m.group(1), source_rel, idx)
        diffs.append((old, new))
        return new

    def _link_sub(m: re.Match) -> str:
        old = m.group(0)
        new = transform_wikilink(m.group(1), source_rel, idx)
        diffs.append((old, new))
        return new

    masked = EMBED_RE.sub(_embed_sub, masked)
    masked = WIKILINK_RE.sub(_link_sub, masked)
    new_text = unmask_code(masked, code_originals)
    return new_text, diffs


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert Obsidian wikilinks to standard markdown.")
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--file", help="Single file (relative or absolute)")
    sel.add_argument("--tier", help="Single tier directory under zk/")
    sel.add_argument("--all", action="store_true", help="All tracked .md files")

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print diffs only")
    mode.add_argument("--apply", action="store_true", help="Write changes back")

    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet", action="store_true", help="Suppress per-file diff output")
    args = ap.parse_args()

    print(f"[index] building stem index from {OV}", file=sys.stderr)
    idx = build_stem_index(OV)
    total_indexed = sum(len(v) for v in idx.values())
    print(f"[index] {total_indexed} files, {len(idx)} distinct stems", file=sys.stderr)

    # Resolve target files
    if args.file:
        p = Path(args.file)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        files = [p]
    elif args.tier:
        tier_path = OV / args.tier
        if not tier_path.is_dir():
            print(f"[error] tier not found: {tier_path}", file=sys.stderr)
            sys.exit(1)
        files = [
            f for f in sorted(tier_path.rglob("*.md"))
            if not any(part in SKIP_DIRS for part in f.relative_to(OV).parts)
        ]
    else:
        files = [
            f for f in sorted(OV.rglob("*.md"))
            if not any(part in SKIP_DIRS for part in f.relative_to(OV).parts)
        ]

    if args.limit:
        files = files[: args.limit]

    print(f"[scan] {len(files)} files selected", file=sys.stderr)

    files_changed = 0
    total_changes = 0
    stats = {
        "link_resolved": 0,
        "tag_unresolved": 0,
        "date_to_daily": 0,
        "date_to_text": 0,
        "stripped_empty": 0,
        "stripped_pure_digit": 0,
        "image_embed": 0,
    }

    def classify(old: str, new: str) -> None:
        if old.startswith("![["):
            stats["image_embed"] += 1
        elif new.startswith("[") and "](" in new:
            # could be link_resolved or date_to_daily
            if "daily-notes" in new:
                stats["date_to_daily"] += 1
            else:
                stats["link_resolved"] += 1
        elif new.startswith("#"):
            stats["tag_unresolved"] += 1
        elif new == "":
            stats["stripped_empty"] += 1
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", new):
            stats["date_to_text"] += 1
        else:
            stats["stripped_pure_digit"] += 1

    for f in files:
        new_text, diffs = transform_file(f, idx)
        if not diffs:
            continue
        files_changed += 1
        total_changes += len(diffs)
        for old, new in diffs:
            classify(old, new)
        rel = f.resolve().relative_to(OV)
        if not args.quiet:
            print(f"\n=== {rel} ({len(diffs)} changes) ===")
            for old, new in diffs:
                print(f"  - {old}")
                print(f"  + {new}")
        if args.apply:
            atomic_write(f, new_text, newline="")

    action = "applied" if args.apply else "dry-run"
    print(f"\n[done] {files_changed} files changed, {total_changes} replacements ({action})",
          file=sys.stderr)
    print(f"[stats] link_resolved:       {stats['link_resolved']}", file=sys.stderr)
    print(f"        tag_unresolved:      {stats['tag_unresolved']}", file=sys.stderr)
    print(f"        date_to_daily:       {stats['date_to_daily']}", file=sys.stderr)
    print(f"        date_to_text:        {stats['date_to_text']}", file=sys.stderr)
    print(f"        image_embed:         {stats['image_embed']}", file=sys.stderr)
    print(f"        stripped_empty:      {stats['stripped_empty']}", file=sys.stderr)
    print(f"        stripped_pure_digit: {stats['stripped_pure_digit']}", file=sys.stderr)


if __name__ == "__main__":
    main()
