#!/usr/bin/env python3
"""Repair broken Markdown links after file moves, or convert them for Reflect.

Unresolved document and image targets are matched by filename and rewritten
relative to the source file. `--to-reflect` also turns note links into the
`[[Title]]` form Reflect backlinks and renames (`[[/folder/Note]]` when a
title is ambiguous, `[[#Heading]]` within a note), and re-encodes the `<...>`
destinations it leaves as paths, which Reflect cannot open.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from _paths import atomic_write, tier_segments, vault_root  # type: ignore[import-not-found]  # noqa: E402
from _reflect import FORBIDDEN_RE, TitleIndex  # type: ignore[import-not-found]  # noqa: E402

OV = vault_root()
SKIP_DIRS = {"secure", "cache", ".obsidian", ".trash", "raw", "assets"}

LINK_RE = re.compile(r"(!?\[)([^\]]*)(\]\()(<[^>\n]+>|(?:[^()\n]|\([^()\n]*\))+)(\))")
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)")
TRACKED_EXTS = (".md", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


def is_tracked_path(rel: Path) -> bool:
    return not any(p in SKIP_DIRS for p in rel.parts)


def build_index(zk: Path) -> dict[str, list[Path]]:
    """name (lowercased) → list of paths (relative to zk)."""
    idx: dict[str, list[Path]] = defaultdict(list)
    for f in zk.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in TRACKED_EXTS:
            continue
        rel = f.relative_to(zk)
        if not is_tracked_path(rel):
            continue
        idx[f.name.lower()].append(rel)
    return idx


def resolve_target(name: str, source_rel: Path, idx: dict[str, list[Path]]) -> Optional[Path]:
    """Look up file name in index; return best match (source-tier-aware)."""
    matches = idx.get(name.lower(), [])
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


def encode_href(path: str) -> str:
    """Reflect clicks a %-encoded destination, never a <...>-wrapped one; a
    colon in the first segment would read as a URI scheme."""
    href = path.replace("%", "%25").replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"./{href}" if ":" in href.split("/", 1)[0] else href


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
        text = text.replace(f"\x00CODE_{i}\x00", originals[i])
    return text


def wikilink(
    text: str, target: Path | None, titles: TitleIndex, table_row: bool, fragment: str = ""
) -> tuple[str | None, str]:
    """The `[[Title]]` for a note link, or None and the reason it stays a path.

    `target` None links the note itself (`[[#Heading]]`); a note whose title
    Reflect cannot address uniquely gets the rooted path `[[/folder/Note]]`.
    Reflect opens the note for a fragment but does not scroll to it.
    """
    reason = "converted"
    if target is None:
        name = ""
    elif (title := titles.title(target)) is None and "secure" not in target.parts:
        return None, "outside_reflect"
    elif title is None or FORBIDDEN_RE.search(title) or titles.resolve(title) != target:
        # Reflect indexes secure notes too; their titles stay unread, so the path names them.
        name, reason = "/" + target.with_suffix("").as_posix(), "converted_path"
    else:
        name = title
    if FORBIDDEN_RE.search(name) or FORBIDDEN_RE.search(fragment):
        return None, "path_chars"
    link = name + (f"#{fragment}" if fragment else "")
    if text in ("", link):
        return f"[[{link}]]", reason
    if "\n" in text:  # Reflect ends [[...]] at a line break
        return None, "display_text"
    if table_row:  # an alias pipe would split the cell, so the target shows
        return f"[[{link}]]", "converted_table"
    if "[" in text:
        return None, "display_text"
    return f"[[{link}|{text.replace('|', '·')}]]", reason  # an alias cannot hold a pipe


def relink_file(
    path: Path,
    idx: dict[str, list[Path]],
    titles: TitleIndex | None = None,
    reasons: Counter | None = None,
    skips: list[tuple[str, str]] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Returns (new_text, list of (old_link, new_link) diffs); `skips` collects
    (link, reason) for note links left as paths."""
    with path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    source_rel = path.relative_to(OV)
    diffs: list[tuple[str, str]] = []
    masked, originals = mask_code(text)

    def _sub(m: re.Match) -> str:
        bang_open, link_text, middle, raw, paren_close = m.groups()
        wrapped = raw.startswith("<")
        href, separator, fragment = raw.strip().removeprefix("<").removesuffix(">").partition("#")
        anchor = separator + fragment
        line_start = masked.rfind("\n", 0, m.start()) + 1
        table_row = masked[line_start:].lstrip().startswith("|")
        new_link = None
        if not href and fragment and titles is not None and reasons is not None and bang_open == "[":
            new_link, reason = wikilink(link_text.strip(), None, titles, table_row, unquote(fragment))
            reasons[reason] += 1
            if new_link is None and skips is not None:
                skips.append((m.group(0), reason))
        if new_link is not None:
            diffs.append((m.group(0), new_link))
            return new_link
        if not href or href.startswith(("mailto:", "/")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", href):
            return m.group(0)
        decoded = unquote(href)
        target_abs = Path(os.path.normpath(path.parent / decoded))  # keep raw/ and secure/ links in-vault
        try:
            target_rel = target_abs.relative_to(OV)
        except ValueError:
            return m.group(0)
        repaired = None
        if not target_abs.exists() and decoded.lower().endswith(TRACKED_EXTS):
            repaired = resolve_target(Path(decoded).name, source_rel, idx)
            if repaired is not None:
                target_rel = repaired
        if titles is not None and reasons is not None:
            if bang_open == "[" and target_rel.suffix.lower() == ".md":
                if not (OV / target_rel).exists():
                    reason = "missing_target"
                else:
                    new_link, reason = wikilink(link_text.strip(), target_rel, titles, table_row, unquote(fragment))
                reasons[reason] += 1
                if new_link is None and skips is not None:
                    skips.append((m.group(0), reason))
            # A destination naming no vault file, such as <tel:...>, stays as written.
            if new_link is None and (wrapped or repaired is not None) and (OV / target_rel).exists():
                new_link = f"{bang_open}{link_text}{middle}{encode_href(relative_path(target_rel, source_rel) + anchor)}{paren_close}"
        elif repaired is not None:
            new_link = f"{bang_open}{link_text}{middle}{encode_href(relative_path(repaired, source_rel) + anchor)}{paren_close}"
        if new_link is None or new_link == m.group(0):
            return m.group(0)
        diffs.append((m.group(0), new_link))
        return new_link

    return unmask_code(LINK_RE.sub(_sub, masked), originals), diffs


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--to-reflect", action="store_true",
                    help="also convert note links to [[Title]] and re-encode <...> paths")
    ap.add_argument("--under", type=Path, help="only rewrite notes under this vault-relative folder")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    print(f"[index] building name index from {OV}", file=sys.stderr)
    idx = build_index(OV)
    print(f"[index] {sum(len(v) for v in idx.values())} files, {len(idx)} distinct names",
          file=sys.stderr)
    titles = TitleIndex(OV) if args.to_reflect else None
    reasons: Counter = Counter()
    daily = Path(tier_segments().get("daily_notes", "daily"))

    files = [
        f for f in sorted(OV.rglob("*.md"))
        if is_tracked_path(rel := f.relative_to(OV))
        and (args.under is None or rel.is_relative_to(args.under))
        and not (args.to_reflect and rel.is_relative_to(daily))  # daily notes are user-authored
    ]
    print(f"[scan] {len(files)} tracked .md files", file=sys.stderr)

    files_changed = 0
    total_replacements = 0
    for f in files:
        skips: list[tuple[str, str]] = []
        new_text, diffs = relink_file(f, idx, titles, reasons if args.to_reflect else None, skips)
        if args.to_reflect and args.dry_run and not args.quiet:
            for link, reason in skips:
                print(f"skip\t{reason}\t{f.relative_to(OV)}\t{unmask_code(link, [])}")
        if not diffs:
            continue
        files_changed += 1
        total_replacements += len(diffs)
        rel = f.relative_to(OV)
        if not args.quiet:
            print(f"\n=== {rel} ({len(diffs)} relinks) ===")
            for old, new in diffs:
                print(f"  - {old}")
                print(f"  + {new}")
        if args.apply:
            atomic_write(f, new_text, newline="")

    action = "applied" if args.apply else "dry-run"
    print(f"\n[done] {files_changed} files changed, {total_replacements} relinks ({action})",
          file=sys.stderr)
    if args.to_reflect:
        print("[note links] " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())), file=sys.stderr)


if __name__ == "__main__":
    main()
