#!/usr/bin/env python3
"""Repair broken Markdown links after file moves.

Unresolved document and image targets are matched by filename and rewritten
relative to the source file.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from _paths import atomic_write, vault_root  # type: ignore[import-not-found]  # noqa: E402
from wikilink_to_md import mask_code, relative_path, unmask_code  # noqa: E402

OV = vault_root()
SKIP_DIRS = {"secure", "cache", ".obsidian", ".trash", "raw", "assets"}

LINK_RE = re.compile(r"(!?\[)([^\]]*)(\]\()(<[^>\n]+>|(?:[^()\n]|\([^()\n]*\))+)(\))")
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


def maybe_wrap(path: str) -> str:
    if any(c in path for c in " ()"):
        return f"<{path}>"
    return path


def relink_file(path: Path, idx: dict[str, list[Path]]) -> tuple[str, list[tuple[str, str]]]:
    """Returns (new_text, list of (old_link, new_link) diffs)."""
    with path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    source_rel = path.resolve().relative_to(OV)
    diffs: list[tuple[str, str]] = []

    def _sub(m: re.Match) -> str:
        bracket_open = m.group(1)
        link_text = m.group(2)
        bracket_close = m.group(3)
        href = m.group(4).strip().removeprefix("<").removesuffix(">")
        href, separator, fragment = href.partition("#")
        anchor = separator + fragment
        paren_close = m.group(5)
        if href.startswith(("http://", "https://", "mailto:", "/", "#")):
            return m.group(0)
        if not any(href.lower().endswith(ext) for ext in TRACKED_EXTS):
            return m.group(0)
        source_dir = path.parent
        target_abs = (source_dir / href).resolve()
        try:
            _ = target_abs.relative_to(OV)
        except ValueError:
            return m.group(0)
        if target_abs.exists():
            return m.group(0)
        name = Path(href).name
        new_target = resolve_target(name, source_rel, idx)
        if not new_target:
            return m.group(0)
        new_rel = relative_path(new_target, source_rel)
        new_path = maybe_wrap(new_rel + anchor)
        new_link = f"{bracket_open}{link_text}{bracket_close}{new_path}{paren_close}"
        diffs.append((m.group(0), new_link))
        return new_link

    masked, originals = mask_code(text)
    return unmask_code(LINK_RE.sub(_sub, masked), originals), diffs


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    print(f"[index] building name index from {OV}", file=sys.stderr)
    idx = build_index(OV)
    print(f"[index] {sum(len(v) for v in idx.values())} files, {len(idx)} distinct names",
          file=sys.stderr)

    files = [
        f for f in sorted(OV.rglob("*.md"))
        if is_tracked_path(f.relative_to(OV))
    ]
    print(f"[scan] {len(files)} tracked .md files", file=sys.stderr)

    files_changed = 0
    total_replacements = 0
    for f in files:
        new_text, diffs = relink_file(f, idx)
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


if __name__ == "__main__":
    main()
