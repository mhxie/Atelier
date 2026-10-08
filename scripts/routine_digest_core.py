"""routine_digest_core.py: registries, date windows, markdown parsing, and manifest helpers shared by the digest pipeline.
"""

from __future__ import annotations

import sys

import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import fmt, tier_segments  # noqa: E402


MANIFEST_SCHEMA = 1

# Public harness routine whose output is maintenance bookkeeping, not intel.
# Named here (not in the private vault registry) because the name is already
# public in the Prefect service documentation and routine adapter registry.
MAINTENANCE_ROUTINES = {"autoevo-nightly"}

DEFAULT_EXCERPT_CHARS = 800

DEFAULT_MAX_ITEMS = 15

DEFAULT_MAX_FILES = 200

MAX_CONTEXT_SOURCES = 8
_CONTEXT_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

# Default lane for a routine that declares none: keyed on the first segment of
# its output_dir. Unknown segments title-case themselves.
LANE_BY_SEGMENT = {
    "finance": "Finance",
    "inbox": "Tech feed",
    "research": "Research",
    "personal": "Toolcraft",
    "career": "Career",
    "agent-findings": "Findings",
    "wip": "Findings",
}

RESEARCH_LANE = "Research"

# Default attention budget, in lines of the digest's own summary. A routine
# earns space by being worth reading, not by being verbose: an unbounded report
# lets one chatty collector crowd out five terse ones. Per-routine overrides live
# beside `lane` and `include` in the same registry row.
DEFAULT_ROUTINE_LINES = 8

@dataclass
class Routine:
    name: str
    label: str
    output_dir: str
    file_pattern: str
    lane: str
    include: bool
    cron: str = ""
    execution: str = "remote"
    max_lines: int = DEFAULT_ROUTINE_LINES
    context: str | None = None

def load_routines(ov: Path) -> list[Routine]:
    """Parse the private routine registry into Routine rows.

    Rows without an output_dir are skipped: they cannot contribute files.
    """
    config_path = ov / tier_segments().get("private_routines", "_tools/routines") / "registry.toml"
    if not config_path.is_file():
        raise SystemExit(f"routine registry missing: {fmt(config_path)}")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SystemExit(f"routine registry unreadable: {exc!r}") from exc

    routines: list[Routine] = []
    context_keys: set[str] = set()
    for row in config.get("routine", []):
        output_dir = row.get("output_dir")
        if not output_dir:
            continue
        name = str(row.get("name") or output_dir)
        digest_cfg = row.get("digest") or {}
        if not isinstance(digest_cfg, dict):
            digest_cfg = {}
        context = digest_cfg.get("context")
        if context is not None:
            if not isinstance(context, str) or not _CONTEXT_KEY.fullmatch(context):
                raise SystemExit(f"{name}: digest.context must be a safe lowercase identifier")
            if context in context_keys:
                raise SystemExit(f"duplicate digest.context: {context}")
            context_keys.add(context)
            if len(context_keys) > MAX_CONTEXT_SOURCES:
                raise SystemExit(f"at most {MAX_CONTEXT_SOURCES} digest contexts may be declared")
        include = digest_cfg.get("include")
        if include is None:
            include = name not in MAINTENANCE_ROUTINES
        lane = digest_cfg.get("lane") or default_lane(output_dir)
        raw_lines = digest_cfg.get("max_lines", DEFAULT_ROUTINE_LINES)
        try:
            max_lines = max(1, int(raw_lines))
        except (TypeError, ValueError):
            max_lines = DEFAULT_ROUTINE_LINES
        routines.append(
            Routine(
                name=name,
                label=str(row.get("label") or name),
                output_dir=str(output_dir),
                file_pattern=str(row.get("file_pattern") or "*.md"),
                lane=str(lane),
                include=bool(include),
                cron=str(row.get("cron") or ""),
                execution=str(row.get("execution") or "remote"),
                max_lines=max_lines,
                context=context,
            )
        )
    return routines

def default_lane(output_dir: str) -> str:
    segment = output_dir.strip("/").split("/", 1)[0]
    return LANE_BY_SEGMENT.get(segment, segment.replace("-", " ").replace("_", " ").title())

def load_acks(ov: Path) -> dict[str, str]:
    path = ov / "_meta" / "routine_acks.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

def humanize_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip()

def _vault_relative(ov: Path, path: Path) -> str:
    try:
        return str(path.relative_to(ov))
    except ValueError:
        return str(path)

def source_anchor(path: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return f"src-{slug}"

def iter_sources(manifest: dict[str, Any]):
    for lane in manifest.get("lanes", []):
        for source in lane.get("sources", []):
            yield lane.get("lane", ""), source

def _within_days(day: str, reference: str, days: int) -> bool:
    try:
        first = date.fromisoformat(day[:10])
        second = date.fromisoformat(reference[:10])
    except ValueError:
        return False
    return 0 <= (second - first).days <= days

def deep_read_lane_gap(deep_read: Any, manifest: dict[str, Any]) -> str | None:
    """One warning line when the depth skipped research on a window that had it.

    The procedure reserves one curated slot for the Research lane whenever the
    window carries a Research source; each entry declares its `lane`. This is
    the deterministic half of that rule: it cannot pick the entry, but it can
    say, on the face of the document, that the pick ignored the primary
    direction. Silent on windows with no Research source and on uncurated
    windows, where there was nothing to reserve.
    """
    if not isinstance(deep_read, dict):
        return None
    entries = [e for e in deep_read.get("entries") or [] if isinstance(e, dict)]
    if not entries:
        return None
    research = [s for lane, s in iter_sources(manifest) if lane == RESEARCH_LANE]
    if not research:
        return None
    if any(e.get("lane") == RESEARCH_LANE for e in entries):
        return None
    return (
        f"情报精选没有 Research 条目, 但本窗口有 {len(research)} 个 Research 来源; "
        "deep_read 至少留一条给研究方向 (entry.lane = \"Research\")"
    )

def note_path(ov: Path, manifest: dict[str, Any]) -> Path:
    """`<paths.digest>/YYYY-MM/<until>-<daily|weekly|backlog>-digest.md`, the one place a note lands."""
    until = str((manifest.get("window") or {}).get("until") or "")
    kind = "backlog" if manifest.get("selection") == "unacked" else str(manifest.get("mode") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until) or kind not in ("daily", "weekly", "backlog"):
        raise SystemExit("manifest needs a daily or weekly mode and a YYYY-MM-DD window.until")
    return ov / tier_segments()["digest"] / until[:7] / f"{until}-{kind}-digest.md"

def routine_files(ov: Path, routine: Routine) -> list[Path]:
    """A row's matching files by name, never the digest's own notes, however the row reaches them.

    The digest folder is matched by identity (device and inode), not by spelling,
    so a differently cased, symlinked, or absolute output_dir cannot read it.
    """
    directory = ov / routine.output_dir
    if not directory.is_dir():
        return []
    try:
        own = os.stat(ov / tier_segments()["digest"])
    except OSError:  # no digest folder yet, so no note to keep out
        own = None
    seen: dict[Path, bool] = {}

    def inside(folder: Path) -> bool:
        if folder not in seen:
            try:
                found = os.path.samestat(os.stat(folder), own)
            except OSError:
                found = False
            seen[folder] = found or (folder.parent != folder and inside(folder.parent))
        return seen[folder]

    return sorted((path for path in directory.glob(routine.file_pattern)
                   if own is None or not inside(path.resolve().parent)), key=lambda path: path.name)

def manifest_names_by_dir(manifest: dict[str, Any]) -> dict[str, set[str]]:
    """Filenames the manifest actually carries, keyed by their directory."""
    shown: dict[str, set[str]] = {}
    for lane in manifest.get("lanes") or []:
        for source in lane.get("sources") or []:
            path = source.get("path")
            if isinstance(path, str) and path:
                shown.setdefault(str(Path(path).parent), set()).add(Path(path).name)
    return shown

def hidden_by_ack(
    ov: Path,
    output_dir: str,
    before: str,
    after: str,
    shown: set[str],
) -> list[tuple[str, int]]:
    """Files an ack would mark reviewed without the reader having seen them.

    The ack is a per-directory mark, so in a directory shared by several
    routines (or by one the digest excludes) it also covers files that were
    never in the manifest. Returns (label, count) per affected routine so the
    reader can decide with that in view rather than discover it from a cue
    that went quiet.
    """
    directory = ov / output_dir
    if not directory.is_dir():
        return []
    hidden: list[tuple[str, int]] = []
    for routine in load_routines(ov):
        if routine.output_dir != output_dir:
            continue
        count = sum(
            1
            for path in routine_files(ov, routine)
            if before < path.name <= after and path.name not in shown
        )
        if count:
            hidden.append((routine.label, count))
    return hidden
