"""Registry-backed path helpers for the Atelier vault.

Paths resolve through the committed registry plus its optional local overlay.
An unset ``$OV`` fails loudly; no helper falls back to a relative vault.
"""

from __future__ import annotations

import errno
import os
import re
import sys
import tempfile
import time
import tomllib
from functools import lru_cache
from pathlib import Path


class PathsError(SystemExit):
    """Path-registry failure.

    Subclasses SystemExit so an unhandled error still exits a CLI with the
    message (the historical behavior), while libraries, smoke checks, and
    in-process tests can catch a TYPED error instead of a bare SystemExit
    string killing the host process.
    """


def reset() -> None:
    """Clear process-wide caches (for in-process tests that change $OV)."""
    vault_root.cache_clear()
    _registry.cache_clear()


@lru_cache(maxsize=1)
def vault_root() -> Path:
    """Return $OV as an absolute Path. Exit with a clear error if unset.

    Refusing to fall back to a relative 'zk/' default because that silently
    creates stray directories wherever the script runs.
    """
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
    """Load harness/paths.toml, layer harness/paths.local.toml on top.

    Returns the merged `[paths]` table. Missing files are tolerated: an
    OSS user without a `paths.local.toml` just gets the canonical map.

    Layering semantics: scalar keys are overridden; the
    `wiki_localized` sub-table is unioned (per-user adds languages
    without losing canonical entries, which are empty by default).
    """
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
    """Resolve a registry segment to an absolute Path under `root` ($OV by default).

    Absolute segments (rare; used for sandbox overrides) pass through.
    """
    if segment.startswith("/"):
        return Path(segment).expanduser().resolve()
    return (root if root is not None else vault_root()) / segment


def tier(name: str) -> Path:
    """Return the absolute Path for a registry tier name.

    Exits with a clear error if the tier is unknown so typos surface
    immediately rather than silently writing to the wrong location.
    """
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
    """Return recursive matches sorted by filename/path, or [] for a missing tier.

    Fission puts files in buckets: plain glob silently misses them. Filename
    sorting keeps date-prefixed files chronological across those buckets.
    """
    root = tier(name)
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.rglob(pattern) if p.is_file()),
        key=lambda p: (p.name, p.as_posix()),
    )


def tier_segments() -> dict[str, str]:
    """Return the merged {name: segment} mapping (no path resolution).

    Useful for lint checks that compare segments against directory names
    in committed markdown.
    """
    reg = _registry()
    return {k: v for k, v in reg.items() if isinstance(v, str)}


def wiki_dirs() -> list[Path]:
    """Return the list of wiki directories: primary + localized.

    Order: primary `wiki` first, then localized entries in dict order
    (Python 3.7+ preserves insertion order). Callers that just need the
    primary directory should call `tier("wiki")` instead.
    """
    reg = _registry()
    dirs = [_resolve_segment(reg["wiki"])]
    for segment in reg.get("wiki_localized", {}).values():
        if isinstance(segment, str):
            dirs.append(_resolve_segment(segment))
    return dirs


def atomic_write(path: Path, text: str, *, fsync: bool = True, newline: str | None = None) -> None:
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
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_DATE_IN_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_iso_date(value: object):
    """`YYYY-MM-DD` (a longer ISO timestamp is truncated) to a date, else None.

    Seven scripts each parsed dates with their own None-vs-exit failure mode;
    the shared contract is: garbage in, None out, and the caller decides
    whether None is fatal.
    """
    from datetime import date

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
    """First `YYYY-MM-DD` found anywhere in the text, else None.

    The search sibling of `parse_iso_date`: five scripts each carried their own
    filename or date-column regex with this same skip-on-miss contract.
    """
    if value is None:
        return None
    match = _DATE_IN_TEXT.search(str(value))
    return parse_iso_date(match.group(0)) if match else None


def fmt(p: Path) -> str:
    """Render under-vault paths as '$OV/<rel>' for token-efficient output.

    Use in stdout, JSON output, error messages: anywhere paths reach the
    orchestrator or user. Internal file operations should keep using the
    absolute Path object directly.

    Falls through to the absolute path string if `p` is not under the vault
    (so logs of out-of-vault paths still resolve unambiguously).
    """
    try:
        rel = p.resolve().relative_to(vault_root())
        return f"$OV/{rel.as_posix()}"
    except ValueError:
        return p.as_posix()


# The vault lives on a Google Drive File Provider mount that intermittently
# answers a read or an flock with EDEADLK while it materializes or syncs the
# file. It is transient, not a real deadlock: the same path reads cleanly a
# moment later. Untreated it has failed routine lock acquisition and aborted a
# nightly sweep mid-plan.
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
