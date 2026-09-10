#!/usr/bin/env python3
"""Safely insert one date-only meal-history reference into a trip-note section."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from _paths import tier


_LOCAL_LOCKS: dict[Path, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock(path: Path) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(path, threading.Lock())


def _section_bounds(text: str, heading: str) -> tuple[int, int, int] | None:
    lines = text.splitlines(keepends=True)
    target = heading.rstrip("\n")
    if not target.startswith("#") or not target.lstrip("#").startswith(" "):
        return None
    level = len(target) - len(target.lstrip("#"))
    offset = 0
    for index, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        if bare == target:
            start = offset + len(line)
            cursor = start
            for following in lines[index + 1 :]:
                candidate = following.rstrip("\r\n")
                candidate_level = len(candidate) - len(candidate.lstrip("#"))
                if candidate_level and candidate_level <= level and candidate[candidate_level:].startswith(" "):
                    return start, cursor, level
                cursor += len(following)
            return start, len(text), level
        offset += len(line)
    return None


def section_content(text: str, heading: str) -> str | None:
    bounds = _section_bounds(text, heading)
    if bounds is None:
        return None
    start, end, _ = bounds
    return text[start:end]


def section_sha256(text: str, heading: str) -> str | None:
    content = section_content(text, heading)
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@contextmanager
def _advisory_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _local_lock(lock_path):
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def lock_file_for(canonical_trip_note: Path) -> Path:
    key = hashlib.sha256(str(canonical_trip_note).encode("utf-8")).hexdigest()
    return tier("cache").resolve() / "trip-reference-locks" / f"{key}.lock"


def _replace_durably(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _insert_at_anchor(section: str, anchor: str, position: str, reference: str, ending: str) -> str | None:
    lines = section.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == anchor]
    if len(matches) != 1:
        return None
    insert_at = matches[0] if position == "before" else matches[0] + 1
    if insert_at and not lines[insert_at - 1].endswith(("\r", "\n")):
        lines[insert_at - 1] += ending
    lines.insert(insert_at, f"{reference}{ending}")
    return "".join(lines)


def insert_trip_reference(
    trip_note: Path,
    section_heading: str,
    expected_section_sha256: str,
    anchor: str,
    position: str,
    reference: str,
) -> dict[str, str]:
    """Lock, validate, and insert exactly one rendered reference."""
    try:
        resolved = trip_note.resolve(strict=True)
        if not resolved.is_file():
            return {"status": "error", "reason": "trip note is not a file"}
        if position not in {"before", "after"}:
            return {"status": "error", "reason": "invalid position"}
        if not reference or "\n" in reference or "\r" in reference:
            return {"status": "error", "reason": "reference must be one line"}
        if not anchor or "\n" in anchor or "\r" in anchor:
            return {"status": "error", "reason": "anchor must be one line"}

        with _advisory_lock(lock_file_for(resolved)):
            with resolved.open(encoding="utf-8", newline="") as handle:
                text = handle.read()
            bounds = _section_bounds(text, section_heading)
            if bounds is None:
                return {"status": "drift", "reason": "section missing"}
            start, end, _ = bounds
            section = text[start:end]
            if reference in section.splitlines():
                return {"status": "already_present"}
            actual_hash = hashlib.sha256(section.encode("utf-8")).hexdigest()
            if actual_hash != expected_section_sha256:
                return {"status": "drift", "reason": "section hash changed"}
            ending = "\r\n" if "\r\n" in text else "\n"
            updated_section = _insert_at_anchor(section, anchor, position, reference, ending)
            if updated_section is None:
                return {"status": "anchor_missing"}
            _replace_durably(resolved, f"{text[:start]}{updated_section}{text[end:]}")
            return {"status": "inserted"}
    except Exception as error:  # CLI contract: report failures as structured data.
        return {"status": "error", "reason": str(error)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trip-note", required=True)
    parser.add_argument("--section-heading", required=True)
    parser.add_argument("--section-sha256", required=True)
    parser.add_argument("--anchor", required=True)
    parser.add_argument("--position", choices=("before", "after"), required=True)
    parser.add_argument("--reference", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = insert_trip_reference(
        Path(args.trip_note),
        args.section_heading,
        args.section_sha256,
        args.anchor,
        args.position,
        args.reference,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
