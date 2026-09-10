#!/usr/bin/env python3
"""Bucket a directory by first letter, year-month, or split year/month.

Only immediate Markdown children move unless ``--include-dirs`` is set.
Existing bucket contents stay in place; link repair remains ``relink.py``'s
responsibility.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional, Union

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from _paths import vault_root  # type: ignore[import-not-found]  # noqa: E402

OV = vault_root()


def axis_first_letter(path: Path) -> str:
    stem = path.stem
    if not stem:
        return "_"
    c = stem[0]
    if c.isascii() and c.isalpha():
        return c.upper()
    if c.isdigit():
        return "0-9"
    return "CJK"


def axis_year_month(path: Path) -> Optional[str]:
    m = re.match(r"^(\d{4})-(\d{2})", path.stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def axis_year_month_split(path: Path) -> Optional[tuple[str, str]]:
    m = re.match(r"^(\d{4})-(\d{2})", path.stem)
    if m:
        return (m.group(1), m.group(2))
    return None


AxisFn = Callable[[Path], Union[str, tuple[str, str], None]]
AXES: dict[str, AxisFn] = {
    "first-letter": axis_first_letter,
    "year-month": axis_year_month,
    "year-month-split": axis_year_month_split,
}


def plan_moves(
    target_dir: Path,
    axis_fn: AxisFn,
    include_dirs: bool = False,
) -> tuple[list[tuple[Path, Path]], list[Path]]:
    """Plan moves. Returns (moves, unmoved) where unmoved are entries the
    axis couldn't bucket (e.g., year-month axis on a non-dated filename).

    If include_dirs=True, also bucket immediate subdirectories (e.g.,
    <dir>/<entry>/ → <dir>/<bucket>/<entry>/ under the chosen axis)."""
    moves: list[tuple[Path, Path]] = []
    unmoved: list[Path] = []
    bucket_pattern = {
        axis_first_letter: r"[A-Z]|0-9|CJK",
        axis_year_month: r"\d{4}-\d{2}",
        axis_year_month_split: r"\d{4}",
    }.get(axis_fn)
    for f in sorted(target_dir.iterdir()):
        is_md = f.is_file() and f.suffix == ".md"
        is_dir_entry = include_dirs and f.is_dir() and not f.name.startswith(".")
        if not (is_md or is_dir_entry):
            continue
        if is_dir_entry and bucket_pattern and re.fullmatch(bucket_pattern, f.name):
            continue
        bucket = axis_fn(f)
        if bucket is None:
            unmoved.append(f)
            continue
        if isinstance(bucket, tuple):
            dst = target_dir.joinpath(*bucket, f.name)
        else:
            dst = target_dir / bucket / f.name
        if dst.exists() or dst.is_symlink():
            raise ValueError(f"destination collision: {dst}")
        for parent in dst.parents:
            if parent == target_dir:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError(f"invalid bucket path: {parent}")
        moves.append((f, dst))
    return moves, unmoved


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="Target directory (relative or absolute)")
    ap.add_argument("--axis", required=True, choices=list(AXES))
    ap.add_argument("--include-dirs", action="store_true",
                    help="Also bucket immediate subdirs (not just .md files)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    target = Path(args.dir).resolve()
    if not target.is_dir():
        print(f"[error] not a directory: {target}", file=sys.stderr)
        sys.exit(1)

    axis_fn = AXES[args.axis]
    try:
        moves, unmoved = plan_moves(target, axis_fn, include_dirs=args.include_dirs)
    except (OSError, ValueError) as exc:
        sys.exit(f"[error] {exc}")
    print(f"[plan] {len(moves)} files to move, {len(unmoved)} unmovable", file=sys.stderr)

    buckets: dict[str, int] = {}
    for _, dst in moves:
        bucket_name = "/".join(dst.relative_to(target).parts[:-1])
        buckets[bucket_name] = buckets.get(bucket_name, 0) + 1
    print(f"[plan] {len(buckets)} buckets:", file=sys.stderr)
    for b, n in sorted(buckets.items()):
        print(f"        {b}/  ({n} files)", file=sys.stderr)
    if unmoved:
        print("[plan] unmovable (no axis match):", file=sys.stderr)
        for f in unmoved[:5]:
            print(f"        {f.name}", file=sys.stderr)
        if len(unmoved) > 5:
            print(f"        ... +{len(unmoved) - 5} more", file=sys.stderr)

    if args.apply:
        for src, dst in moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                sys.exit(f"[error] destination collision: {dst}")
            shutil.move(str(src), str(dst))
        print(f"[apply] moved {len(moves)} files", file=sys.stderr)
        print("[next] run: uv run scripts/relink.py --apply", file=sys.stderr)
    else:
        print(f"[dry-run] would move {len(moves)} files", file=sys.stderr)


if __name__ == "__main__":
    main()
