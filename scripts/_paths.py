"""Vault paths from the canonical registry and optional local overlay."""

from __future__ import annotations

import errno
import os
import re
import sys
import tempfile
import time
import tomllib
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path


class PathsError(SystemExit):
    """Catchable registry error retaining the CLI's SystemExit behavior."""


def reset() -> None:
    """Clear process-wide caches (for in-process tests that change $OV)."""
    vault_root.cache_clear()
    _registry.cache_clear()


@lru_cache(maxsize=1)
def vault_root() -> Path:
    """Return absolute $OV; an unset value fails instead of creating a relative vault."""
    ov = os.environ.get("OV")
    if not ov:
        prog = Path(sys.argv[0]).name if sys.argv else "<script>"
        raise PathsError(
            f"ERROR: $OV environment variable not set. "
            f"Set it to your vault root before running {prog} "
            f'(e.g., `export OV="$HOME/zk"`).'
        )
    return Path(ov).expanduser().resolve()


def _atelier_root() -> Path:
    """Repo root (one level above scripts/)."""
    return Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def _registry() -> dict:
    """Merge local scalar overrides and unioned wiki_localized entries into [paths]."""
    canonical_path = _atelier_root() / "harness" / "paths.toml"
    if not canonical_path.is_file():
        raise PathsError(
            f"ERROR: canonical path registry missing at {canonical_path}. "
            "The atelier repo is incomplete; restore harness/paths.toml."
        )
    with canonical_path.open("rb") as f:
        merged = tomllib.load(f).get("paths", {})
    merged.setdefault("wiki_localized", {})

    local_path = _atelier_root() / "harness" / "paths.local.toml"
    if local_path.is_file():
        with local_path.open("rb") as f:
            local = tomllib.load(f).get("paths", {})
        local_loc = local.pop("wiki_localized", None)
        merged.update(local)
        if local_loc:
            merged["wiki_localized"] = {
                **merged.get("wiki_localized", {}),
                **local_loc,
            }

    return merged


def _resolve_segment(segment: str, root: Path | None = None) -> Path:
    """Resolve under root/$OV, preserving absolute sandbox overrides."""
    if segment.startswith("/"):
        return Path(segment).expanduser().resolve()
    return (root if root is not None else vault_root()) / segment


def tier(name: str) -> Path:
    """Resolve a tier to an absolute path; unknown names fail."""
    reg = _registry()
    if name not in reg:
        known = sorted(k for k in reg if k != "wiki_localized")
        raise PathsError(
            f"ERROR: unknown tier '{name}' in path registry. "
            f"Known: {', '.join(known)}. "
            "Add it to harness/paths.toml (or paths.local.toml for "
            "per-user tiers)."
        )
    value = reg[name]
    if not isinstance(value, str):
        raise PathsError(
            f"ERROR: tier '{name}' resolves to {type(value).__name__}, "
            "expected string. Check harness/paths.toml."
        )
    return _resolve_segment(value)


def tier_files(name: str, pattern: str = "*.md") -> list[Path]:
    """Match recursively across buckets, ordered by filename/path; missing tiers return []."""
    root = tier(name)
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.rglob(pattern) if p.is_file()),
        key=lambda p: (p.name, p.as_posix()),
    )


def tier_segments() -> dict[str, str]:
    """Return merged registry segments without resolving paths."""
    reg = _registry()
    return {k: v for k, v in reg.items() if isinstance(v, str)}


def wiki_dirs() -> list[Path]:
    """Return the primary wiki followed by localized directories in registry order."""
    reg = _registry()
    dirs = [_resolve_segment(reg["wiki"])]
    for segment in reg.get("wiki_localized", {}).values():
        if isinstance(segment, str):
            dirs.append(_resolve_segment(segment))
    return dirs


def knowledge_levels(root: Path | None = None) -> dict:
    """Export Reflect's derived classification without reading note contents."""
    root = root if root is not None else vault_root()
    with (_atelier_root() / "harness" / "paths.toml").open("rb") as handle:
        levels = tomllib.load(handle)["knowledge_levels"]
    registry, definitions, rules = _registry(), [], []
    for number, spec in levels.items():
        level = int(number)
        definitions.append({"level": level, "label": spec["label"]})
        entries = [(registry[name], {}) for name in spec["paths"]]
        if level == 4:
            entries += [(path, {"role": "shadow"}) for path in registry["wiki_localized"].values()]
        for segment, extra in entries:
            if ".." in Path(segment).parts or "\\" in segment or ":" in segment:
                raise PathsError(f"ERROR: invalid knowledge path: {segment!r}")
            try:
                path = (root / segment).relative_to(root).as_posix()
            except ValueError:
                continue  # Out-of-vault absolute overrides have no graph-relative rule.
            if path == ".":
                raise PathsError("ERROR: a knowledge tier cannot classify the whole vault")
            rules.append({"path": path, "match": "tree", "level": level, **extra})
        rules += [{"path": segment, "match": "segment", "level": level} for segment in spec.get("segments", [])]
    assignments = {}
    for rule in rules:
        key = (rule["match"], rule["path"])
        if key in assignments and assignments[key] != rule:
            raise PathsError(f"ERROR: conflicting knowledge levels for {rule['path']!r}")
        assignments[key] = rule
    return {"version": 1, "levels": definitions, "rules": list(assignments.values())}


def raw_store() -> Path | None:
    """Return the optional out-of-vault mirror behind `raw/` and `secure/` symlinks, else None."""
    value = _registry().get("raw_store")
    if value is not None and not (isinstance(value, str) and value.startswith("/")):
        raise PathsError("ERROR: raw_store must be an absolute path in harness/paths.local.toml.")
    return None if value is None else _resolve_segment(value)


def atomic_write(
    path: Path, text: str, *, fsync: bool = True, newline: str | None = None,
    expected_text: str | None = None,
) -> None:
    """Replace a complete file using a unique sibling, preserving existing permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode: int | None = None
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = None
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline=newline) as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(text)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        if expected_text is not None and path.read_bytes().decode("utf-8") != expected_text:
            raise ValueError(f"{path}: changed before replacement; no write performed")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_DATE_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_iso_date(value: object):
    """Parse YYYY-MM-DD or an ISO timestamp's date prefix; invalid input returns None."""
    if value is None:
        return None
    text = str(value).strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def date_in_text(value: object):
    """Return the first YYYY-MM-DD in text, else None."""
    if value is None:
        return None
    match = _DATE_IN_TEXT.search(str(value))
    return parse_iso_date(match.group(0)) if match else None


def effective_date(now: datetime | None = None) -> date:
    """Today, or yesterday before 03:00 local -- the harness day boundary."""
    now = now or datetime.now()
    return now.date() - timedelta(days=1) if now.hour < 3 else now.date()


def fmt(p: Path) -> str:
    """Render under-vault paths as $OV/<rel>; preserve absolute paths elsewhere."""
    try:
        rel = p.resolve().relative_to(vault_root())
        return f"$OV/{rel.as_posix()}"
    except ValueError:
        return p.as_posix()


# File Provider may transiently return EDEADLK while materializing a file.
TRANSIENT_MOUNT_ERRNOS = frozenset({errno.EDEADLK, errno.EAGAIN})


def retry_transient(operation, *, attempts: int = 4, delay: float = 0.5, what: str = "vault operation"):
    """Run ``operation``, retrying the mount's transient EDEADLK with backoff.

    Any other ``OSError`` is re-raised immediately: this widens no failure
    except the one the mount is known to invent.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except OSError as exc:
            if exc.errno not in TRANSIENT_MOUNT_ERRNOS or attempt == attempts:
                raise
            print(
                f"warning: {what} hit transient mount error {exc.errno} "
                f"(attempt {attempt}/{attempts}); retrying",
                file=sys.stderr,
            )
            time.sleep(delay * attempt)
    raise AssertionError("unreachable")
