#!/usr/bin/env python3
"""Build a bounded, route-specific context projection from a local OV vault.

The helper is deliberately read-only.  It resolves an already-selected intent,
loads that intent's current ``profile_reads`` declaration, and emits excerpts
to stdout.  It never persists the generated projection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTENTS_PATH = ROOT / "harness" / "intents.toml"
DEFAULT_COMPONENTS = ("profile", "session", "reflections")
VALID_COMPONENTS = ("profile", "session", "reflections", "daily", "sources")
MAX_BYTE_BUDGET = 64 * 1024
DEFAULT_REFLECTION_COUNT = 3
MAX_REFLECTION_COUNT = 10

SESSION_SECTION_NAMES = ("Continuity", "Anomalies")
READING_CAPSULE_SECTION = "Reading Capsule"
REFLECTION_HEADING_LIMIT = 40
REFLECTION_CLOSING_LIMIT = 2
# Smallest excerpt worth emitting. Below this a fragment is noise; the
# candidate is omitted (and reported) instead of stubbed.
MIN_EXCERPT_BYTES = 256

# Per-candidate ceilings. They exist so one oversized file cannot consume the
# whole projection, not to ration context: at these sizes a typical profile
# file, session section, or reflection closing lands whole.
PROFILE_CAP_BYTES = 16 * 1024
SESSION_SECTION_CAP_BYTES = 4 * 1024
REFLECTION_HEADINGS_CAP_BYTES = 2 * 1024
REFLECTION_CLOSING_CAP_BYTES = 4 * 1024
DAILY_CAP_BYTES = 16 * 1024
SOURCE_CAP_BYTES = 16 * 1024

COMPONENT_RENDER_ORDER = {
    "profile": 0,
    "session": 1,
    "reflections": 2,
    "daily": 3,
    "source": 4,
}

LOW_SIGNAL_REFLECTION_SECTIONS = {
    "full text",
    "full text anchors",
    "full text of sources",
    "notes referenced",
    "new files created",
    "profile updates",
    "support system log",
    "session meta",
}

HIGH_SIGNAL_CLOSING_PREFIXES = (
    "next action",
    "next actions",
    "next step",
    "next steps",
    "next week",
    "open question",
    "open questions",
    "open decision",
    "open decisions",
    "decision",
    "decisions",
    "unresolved",
    "continuity",
    "commitment",
    "commitments",
    "follow-up",
    "follow up",
    "specific watchables",
    "scope boundary",
    "the landing",
    "landing",
    "seed",
    "下一步",
    "下周",
    "开放问题",
    "待决定",
    "待决策",
    "后续",
    "行动",
)

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-|$)")
SEQUENCE_SUFFIX_RE = re.compile(r"-(\d+)$")


class BundleError(ValueError):
    """A user-facing context bundle input or source error."""


@dataclass(frozen=True)
class MarkdownSection:
    level: int
    title: str
    body: str
    line: int


@dataclass(frozen=True)
class Candidate:
    component: str
    source: str
    section: str
    representation: str
    text: str
    source_bytes: int
    cap_bytes: int
    priority: int
    ordinal: int
    pre_truncation_reasons: tuple[str, ...] = ()

    @property
    def available_bytes(self) -> int:
        return utf8_len(self.text)


@dataclass
class Excerpt:
    candidate: Candidate
    content: str
    truncation_reasons: set[str] = field(default_factory=set)

    @property
    def included_bytes(self) -> int:
        return utf8_len(self.content)

    @property
    def truncated(self) -> bool:
        return bool(self.truncation_reasons)


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_utf8(value: str, limit: int) -> str:
    """Return a deterministic prefix no larger than ``limit`` UTF-8 bytes."""
    if limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value

    marker = "\n[truncated]"
    marker_bytes = marker.encode("utf-8")
    if limit <= len(marker_bytes):
        return encoded[:limit].decode("utf-8", errors="ignore")

    prefix_limit = limit - len(marker_bytes)
    prefix = encoded[:prefix_limit].decode("utf-8", errors="ignore").rstrip()
    result = prefix + marker
    while utf8_len(result) > limit and prefix:
        prefix = prefix[:-1].rstrip()
        result = prefix + marker
    return result if utf8_len(result) <= limit else marker[:limit]


def effective_date_today(now: datetime | None = None) -> date:
    """Apply the Atelier late-sleep rule to the local wall clock."""
    current = now or datetime.now().astimezone()
    if current.hour < 3:
        return current.date() - timedelta(days=1)
    return current.date()


def parse_effective_date(value: str | None) -> date:
    if value is None:
        return effective_date_today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BundleError(
            f"invalid --effective-date {value!r}; expected YYYY-MM-DD"
        ) from exc


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_intents(path: Path) -> dict[str, dict[str, Any]]:
    try:
        from registries import RegistryError, load_intents as _shared_load

        intents = _shared_load()
    except ImportError as exc:  # pragma: no cover - registries.py always ships
        raise BundleError(f"cannot import shared registry loader: {exc}") from exc
    except RegistryError as exc:
        raise BundleError(str(exc)) from exc
    rows = {
        str(name): row
        for name, row in intents.items()
        if isinstance(name, str) and isinstance(row, dict)
    }
    # The gitignored overlay next to the registry may add private rows; the
    # same merge the router uses, so `--intent <private>` resolves here too.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from intent_coverage import merge_overlay

        rows, _problems = merge_overlay(rows, path.with_name("intents.local.toml"))
    except ImportError:
        pass
    return rows


def normalize_intent_name(value: str) -> str:
    name = value.strip()
    if name.startswith("intents."):
        name = name[len("intents.") :]
    if not name:
        raise BundleError("selected intent name is empty")
    return name


def _validate_profile_reads(value: Any, intent_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BundleError(
            f"intents.{intent_name}.profile_reads must be a list of filenames"
        )

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise BundleError(
                f"intents.{intent_name}.profile_reads contains a non-string entry"
            )
        filename = item.strip()
        path = Path(filename)
        if (
            not filename
            or path.is_absolute()
            or path.name != filename
            or filename in {".", ".."}
        ):
            raise BundleError(
                f"intents.{intent_name}.profile_reads entry {item!r} "
                "must be one filename under profile/"
            )
        if filename not in seen:
            result.append(filename)
            seen.add(filename)
    return result


def _validate_context_budget(value: Any, intent_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BundleError(
            f"intents.{intent_name}.context_budget_bytes must be an integer"
        )
    if value <= 0 or value > MAX_BYTE_BUDGET:
        raise BundleError(
            f"intents.{intent_name}.context_budget_bytes must be between 1 and "
            f"{MAX_BYTE_BUDGET}"
        )
    return value


def resolve_route(
    *,
    intent_arg: str | None,
    intents_path: Path,
) -> dict[str, Any]:
    intents = load_intents(intents_path)
    if intent_arg is None:
        raise BundleError("--intent is required")
    intent_name = normalize_intent_name(intent_arg)

    row = intents.get(intent_name)
    if row is None:
        raise BundleError(
            f"intent {intent_name!r} is not declared in {display_path(intents_path)}"
        )

    mode = row.get("mode", "")
    if not isinstance(mode, str):
        raise BundleError(f"intents.{intent_name}.mode must be a string")
    procedure = row.get("procedure", "")
    if not isinstance(procedure, str) or not procedure.strip():
        raise BundleError(f"intents.{intent_name}.procedure must be a path string")
    context_budget_bytes = _validate_context_budget(
        row.get("context_budget_bytes"), intent_name
    )
    profile_reads = _validate_profile_reads(row.get("profile_reads", []), intent_name)

    route: dict[str, Any] = {
        "input": "intent",
        "name": intent_name,
        "mode": mode,
        "procedure": procedure,
        "context_budget_bytes": context_budget_bytes,
        "profile_reads": profile_reads,
        "registry": display_path(intents_path),
    }
    return route


def parse_components(values: Sequence[str] | None) -> list[str]:
    if not values:
        return list(DEFAULT_COMPONENTS)

    result: list[str] = []
    for value in values:
        for item in value.split(","):
            component = item.strip().lower()
            if not component:
                continue
            if component not in VALID_COMPONENTS:
                valid = ", ".join(VALID_COMPONENTS)
                raise BundleError(
                    f"unknown component {component!r}; expected one of: {valid}"
                )
            if component not in result:
                result.append(component)
    if not result:
        raise BundleError("--component did not select any components")
    return result


def resolve_vault(value: str | None) -> Path:
    raw = value or os.environ.get("OV")
    if not raw:
        raise BundleError("vault path unavailable; pass --vault or set OV")
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise BundleError(f"vault is not a directory: {vault}")
    return vault


def relative_vault_path(vault: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(vault).as_posix()
    except ValueError as exc:
        raise BundleError(f"source escapes the vault: {path}") from exc


def resolve_source_path(vault: Path, raw_path: str) -> tuple[Path, str]:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = vault / path
    resolved = path.resolve()
    relative = relative_vault_path(vault, resolved)
    return resolved, relative


def read_utf8(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, "missing"
    except IsADirectoryError:
        return None, "is_directory"
    except UnicodeDecodeError:
        return None, "not_utf8"
    except OSError:
        return None, "unreadable"


def parse_markdown_sections(text: str) -> list[MarkdownSection]:
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    active_fence: str | None = None

    for index, line in enumerate(lines):
        fence_match = FENCE_RE.match(line)
        if fence_match:
            fence_char = fence_match.group(1)[0]
            if active_fence is None:
                active_fence = fence_char
            elif active_fence == fence_char:
                active_fence = None
            continue
        if active_fence is not None:
            continue

        heading_match = HEADING_RE.match(line)
        if not heading_match:
            continue
        title = heading_match.group(2).strip().rstrip("#").strip()
        if title:
            headings.append((index, len(heading_match.group(1)), title))

    sections: list[MarkdownSection] = []
    for position, (line_index, level, title) in enumerate(headings):
        end = len(lines)
        for next_index, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        body = "\n".join(lines[line_index + 1 : end]).strip()
        sections.append(
            MarkdownSection(level=level, title=title, body=body, line=line_index + 1)
        )
    return sections


def normalize_heading(value: str) -> str:
    value = re.sub(r"[`*_]", "", value)
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return value


def section_by_title(
    sections: Iterable[MarkdownSection], title: str
) -> MarkdownSection | None:
    target = normalize_heading(title)
    return next(
        (section for section in sections if normalize_heading(section.title) == target),
        None,
    )


def dated_path_key(path: Path) -> tuple[date, int, str] | None:
    match = DATE_PREFIX_RE.match(path.name)
    if not match:
        return None
    try:
        path_date = date.fromisoformat(match.group(1))
    except ValueError:
        return None
    sequence_match = SEQUENCE_SUFFIX_RE.search(path.stem)
    sequence = int(sequence_match.group(1)) if sequence_match else 1
    return path_date, sequence, path.as_posix()


def latest_markdown_paths(
    directory: Path, effective_date: date, count: int
) -> list[Path]:
    if not directory.is_dir():
        return []
    dated: list[tuple[tuple[date, int, str], Path]] = []
    for path in directory.rglob("*.md"):
        if not path.is_file():
            continue
        key = dated_path_key(path)
        if key is not None and key[0] <= effective_date:
            dated.append((key, path))
    dated.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in dated[:count]]


def is_low_signal_reflection_section(title: str) -> bool:
    normalized = normalize_heading(title)
    return any(
        normalized == excluded or normalized.startswith(excluded + " ")
        for excluded in LOW_SIGNAL_REFLECTION_SECTIONS
    )


def reflection_heading_index(
    sections: Sequence[MarkdownSection],
) -> tuple[str, bool]:
    lines: list[str] = []
    current_h2_excluded = False
    omitted = 0
    for section in sections:
        if section.level == 2:
            current_h2_excluded = is_low_signal_reflection_section(section.title)
        if section.level not in (2, 3) or current_h2_excluded:
            continue
        if len(lines) >= REFLECTION_HEADING_LIMIT:
            omitted += 1
            continue
        lines.append(f"{'#' * section.level} {section.title}")
    if omitted:
        lines.append(f"[{omitted} additional headings omitted]")
    return "\n".join(lines), bool(omitted)


def is_high_signal_closing_section(title: str) -> bool:
    normalized = normalize_heading(title)
    return normalized.startswith(HIGH_SIGNAL_CLOSING_PREFIXES)


def omission(
    component: str,
    source: str | None,
    section: str | None,
    reason: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {"component": component, "reason": reason}
    if source is not None:
        result["source"] = source
    if section is not None:
        result["section"] = section
    return result


def add_profile_candidates(
    candidates: list[Candidate],
    omissions: list[dict[str, Any]],
    *,
    vault: Path,
    profile_reads: Sequence[str],
    next_ordinal: int,
) -> int:
    for filename in profile_reads:
        path, relative = resolve_source_path(vault, f"profile/{filename}")
        text, error = read_utf8(path)
        if error is not None:
            omissions.append(omission("profile", relative, "full", error))
            continue
        assert text is not None
        content = text.strip("\n")
        if not content.strip():
            omissions.append(omission("profile", relative, "full", "empty"))
            continue
        candidates.append(
            Candidate(
                component="profile",
                source=relative,
                section="full",
                representation="source_excerpt",
                text=content,
                source_bytes=utf8_len(text),
                cap_bytes=PROFILE_CAP_BYTES,
                priority=10,
                ordinal=next_ordinal,
            )
        )
        next_ordinal += 1
    return next_ordinal


def add_session_candidates(
    candidates: list[Candidate],
    omissions: list[dict[str, Any]],
    *,
    vault: Path,
    effective_date: date,
    route_name: str,
    next_ordinal: int,
) -> int:
    paths = latest_markdown_paths(vault / "sessions", effective_date, 1)
    if not paths:
        omissions.append(omission("session", "sessions/", None, "no_dated_files"))
        return next_ordinal

    path = paths[0]
    relative = relative_vault_path(vault, path)
    text, error = read_utf8(path)
    if error is not None:
        omissions.append(omission("session", relative, None, error))
        return next_ordinal
    assert text is not None
    sections = parse_markdown_sections(text)
    for title in SESSION_SECTION_NAMES:
        section = section_by_title(sections, title)
        if section is None:
            omissions.append(omission("session", relative, title, "section_missing"))
            continue
        if not section.body.strip():
            omissions.append(omission("session", relative, section.title, "empty"))
            continue
        candidates.append(
            Candidate(
                component="session",
                source=relative,
                section=section.title,
                representation="source_section",
                text=section.body,
                source_bytes=utf8_len(text),
                cap_bytes=SESSION_SECTION_CAP_BYTES,
                priority=0,
                ordinal=next_ordinal,
            )
        )
        next_ordinal += 1

    if route_name not in {"reading", "talk"}:
        return next_ordinal

    reading_path: Path | None = None
    reading_key: tuple[date, int, str] | None = None
    for path in (vault / "sessions").rglob("*.md"):
        if not path.is_file() or not re.search(r"-reading(?:-\d+)?$", path.stem):
            continue
        key = dated_path_key(path)
        if key is not None and key[0] <= effective_date and (
            reading_key is None or key > reading_key
        ):
            reading_path = path
            reading_key = key
    if reading_path is None:
        omissions.append(
            omission("session", "sessions/", READING_CAPSULE_SECTION, "no_reading_log")
        )
        return next_ordinal

    reading_relative = relative_vault_path(vault, reading_path)
    reading_text, reading_error = read_utf8(reading_path)
    if reading_error is not None:
        omissions.append(
            omission(
                "session",
                reading_relative,
                READING_CAPSULE_SECTION,
                reading_error,
            )
        )
        return next_ordinal
    assert reading_text is not None
    reading_section = section_by_title(
        parse_markdown_sections(reading_text), READING_CAPSULE_SECTION
    )
    if reading_section is None:
        omissions.append(
            omission(
                "session",
                reading_relative,
                READING_CAPSULE_SECTION,
                "section_missing",
            )
        )
        return next_ordinal
    if not reading_section.body.strip():
        omissions.append(
            omission(
                "session",
                reading_relative,
                reading_section.title,
                "empty",
            )
        )
        return next_ordinal
    candidates.append(
        Candidate(
            component="session",
            source=reading_relative,
            section=reading_section.title,
            representation="source_section",
            text=reading_section.body,
            source_bytes=utf8_len(reading_text),
            cap_bytes=SESSION_SECTION_CAP_BYTES,
            priority=1,
            ordinal=next_ordinal,
        )
    )
    return next_ordinal + 1


def add_reflection_candidates(
    candidates: list[Candidate],
    omissions: list[dict[str, Any]],
    *,
    vault: Path,
    effective_date: date,
    reflection_count: int,
    next_ordinal: int,
) -> int:
    if reflection_count == 0:
        return next_ordinal
    paths = latest_markdown_paths(
        vault / "reflections", effective_date, reflection_count
    )
    if not paths:
        omissions.append(
            omission("reflections", "reflections/", None, "no_dated_files")
        )
        return next_ordinal

    for path in paths:
        relative = relative_vault_path(vault, path)
        text, error = read_utf8(path)
        if error is not None:
            omissions.append(omission("reflections", relative, None, error))
            continue
        assert text is not None
        sections = parse_markdown_sections(text)
        headings, headings_truncated = reflection_heading_index(sections)
        if headings:
            reasons = ("heading_limit",) if headings_truncated else ()
            candidates.append(
                Candidate(
                    component="reflections",
                    source=relative,
                    section="headings",
                    representation="heading_index",
                    text=headings,
                    source_bytes=utf8_len(text),
                    cap_bytes=REFLECTION_HEADINGS_CAP_BYTES,
                    priority=25,
                    ordinal=next_ordinal,
                    pre_truncation_reasons=reasons,
                )
            )
            next_ordinal += 1
        else:
            omissions.append(
                omission("reflections", relative, "headings", "no_headings")
            )

        closing = [
            section
            for section in sections
            if section.level == 2
            and is_high_signal_closing_section(section.title)
            and section.body.strip()
        ][-REFLECTION_CLOSING_LIMIT:]
        for section in closing:
            candidates.append(
                Candidate(
                    component="reflections",
                    source=relative,
                    section=section.title,
                    representation="source_section",
                    text=section.body,
                    source_bytes=utf8_len(text),
                    cap_bytes=REFLECTION_CLOSING_CAP_BYTES,
                    priority=20,
                    ordinal=next_ordinal,
                )
            )
            next_ordinal += 1
        if not closing:
            omissions.append(
                omission(
                    "reflections",
                    relative,
                    "high-signal closing sections",
                    "no_matching_section",
                )
            )
    return next_ordinal


def add_daily_candidate(
    candidates: list[Candidate],
    omissions: list[dict[str, Any]],
    *,
    vault: Path,
    effective_date: date,
    next_ordinal: int,
) -> int:
    relative = (
        f"daily-notes/{effective_date:%Y}/{effective_date:%m}/"
        f"{effective_date.isoformat()}.md"
    )
    path, relative = resolve_source_path(vault, relative)
    text, error = read_utf8(path)
    if error is not None:
        omissions.append(omission("daily", relative, "full", error))
        return next_ordinal
    assert text is not None
    content = text.strip("\n")
    if not content.strip():
        omissions.append(omission("daily", relative, "full", "empty"))
        return next_ordinal
    candidates.append(
        Candidate(
            component="daily",
            source=relative,
            section="full",
            representation="source_excerpt",
            text=content,
            source_bytes=utf8_len(text),
            cap_bytes=DAILY_CAP_BYTES,
            priority=5,
            ordinal=next_ordinal,
        )
    )
    return next_ordinal + 1


def split_source_spec(vault: Path, value: str) -> tuple[str, str | None]:
    """Split ``PATH[#SECTION]`` while allowing literal ``#`` in existing paths."""
    raw = value.strip()
    if not raw:
        raise BundleError("--source cannot be empty")
    full_path = Path(raw).expanduser()
    if not full_path.is_absolute():
        full_path = vault / full_path
    if full_path.exists() or "#" not in raw:
        return raw, None
    path_part, section = raw.rsplit("#", 1)
    if not path_part or not section.strip():
        return raw, None
    return path_part, section.strip()


def add_explicit_source_candidates(
    candidates: list[Candidate],
    omissions: list[dict[str, Any]],
    *,
    vault: Path,
    source_specs: Sequence[str],
    next_ordinal: int,
) -> int:
    for spec in source_specs:
        raw_path, requested_section = split_source_spec(vault, spec)
        path, relative = resolve_source_path(vault, raw_path)
        text, error = read_utf8(path)
        section_label = requested_section or "full"
        if error is not None:
            omissions.append(omission("source", relative, section_label, error))
            continue
        assert text is not None

        representation = "source_excerpt"
        content = text.strip("\n")
        if requested_section is not None:
            section = section_by_title(parse_markdown_sections(text), requested_section)
            if section is None:
                omissions.append(
                    omission("source", relative, requested_section, "section_missing")
                )
                continue
            section_label = section.title
            content = section.body
            representation = "source_section"

        if not content.strip():
            omissions.append(omission("source", relative, section_label, "empty"))
            continue
        candidates.append(
            Candidate(
                component="source",
                source=relative,
                section=section_label,
                representation=representation,
                text=content,
                source_bytes=utf8_len(text),
                cap_bytes=SOURCE_CAP_BYTES,
                priority=5,
                ordinal=next_ordinal,
            )
        )
        next_ordinal += 1
    return next_ordinal


def deduplicate_candidates(
    candidates: Sequence[Candidate], omissions: list[dict[str, Any]]
) -> list[Candidate]:
    result: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.source, candidate.section.casefold())
        if key in seen:
            omissions.append(
                omission(
                    candidate.component,
                    candidate.source,
                    candidate.section,
                    "duplicate_projection",
                )
            )
            continue
        seen.add(key)
        result.append(candidate)
    return result


def gather_candidates(
    *,
    vault: Path,
    route: dict[str, Any],
    components: Sequence[str],
    source_specs: Sequence[str],
    effective_date: date,
    reflection_count: int,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    candidates: list[Candidate] = []
    omissions: list[dict[str, Any]] = []
    ordinal = 0

    if "profile" in components:
        ordinal = add_profile_candidates(
            candidates,
            omissions,
            vault=vault,
            profile_reads=route["profile_reads"],
            next_ordinal=ordinal,
        )
    if "session" in components:
        ordinal = add_session_candidates(
            candidates,
            omissions,
            vault=vault,
            effective_date=effective_date,
            route_name=route["name"],
            next_ordinal=ordinal,
        )
    if "reflections" in components:
        ordinal = add_reflection_candidates(
            candidates,
            omissions,
            vault=vault,
            effective_date=effective_date,
            reflection_count=reflection_count,
            next_ordinal=ordinal,
        )
    if "daily" in components:
        ordinal = add_daily_candidate(
            candidates,
            omissions,
            vault=vault,
            effective_date=effective_date,
            next_ordinal=ordinal,
        )
    if source_specs:
        ordinal = add_explicit_source_candidates(
            candidates,
            omissions,
            vault=vault,
            source_specs=source_specs,
            next_ordinal=ordinal,
        )
    elif "sources" in components:
        omissions.append(omission("source", None, None, "no_sources_requested"))

    del ordinal
    return deduplicate_candidates(candidates, omissions), omissions


def allocate_excerpts(
    candidates: Sequence[Candidate],
    omissions: list[dict[str, Any]],
    byte_budget: int,
) -> list[Excerpt]:
    """Whole sections in priority order until the budget is spent.

    A candidate that fits is included whole (up to its cap). One that does
    not fit is truncated to the remaining space when at least
    MIN_EXCERPT_BYTES remain, otherwise omitted and reported. Nothing is
    pre-shrunk to make room for lower-priority material: one full profile
    beats three stubs.
    """
    ordered = sorted(candidates, key=lambda item: (item.priority, item.ordinal))
    allocations: dict[int, int] = {}
    remaining = byte_budget
    for candidate in ordered:
        grant = min(candidate.cap_bytes, candidate.available_bytes, max(remaining, 0))
        if grant < min(MIN_EXCERPT_BYTES, candidate.available_bytes):
            grant = 0
        allocations[candidate.ordinal] = grant
        remaining -= grant

    excerpts: list[Excerpt] = []
    for candidate in candidates:
        grant = allocations.get(candidate.ordinal, 0)
        if grant <= 0:
            omissions.append(
                omission(
                    candidate.component,
                    candidate.source,
                    candidate.section,
                    "content_budget_exhausted",
                )
            )
            continue
        content = truncate_utf8(candidate.text, grant)
        reasons = set(candidate.pre_truncation_reasons)
        if grant < candidate.available_bytes:
            reasons.add("component_cap" if grant >= candidate.cap_bytes else "content_budget")
        excerpts.append(Excerpt(candidate=candidate, content=content, truncation_reasons=reasons))
    return excerpts


def excerpt_dict(excerpt: Excerpt) -> dict[str, Any]:
    candidate = excerpt.candidate
    result: dict[str, Any] = {
        "component": candidate.component,
        "source": candidate.source,
        "section": candidate.section,
        "representation": candidate.representation,
        "source_bytes": candidate.source_bytes,
        "available_bytes": candidate.available_bytes,
        "included_bytes": excerpt.included_bytes,
        "truncated": excerpt.truncated,
    }
    if excerpt.truncated:
        result["truncation_reasons"] = sorted(excerpt.truncation_reasons)
    result["content"] = excerpt.content
    return result


def payload_dict(
    *,
    route: dict[str, Any],
    effective_date: date,
    components: Sequence[str],
    excerpts: Sequence[Excerpt],
    omissions: Sequence[dict[str, Any]],
    byte_budget: int,
    output_bytes: int,
    suppressed_omissions: int,
) -> dict[str, Any]:
    content_bytes = sum(excerpt.included_bytes for excerpt in excerpts)
    truncated = (
        any(excerpt.truncated for excerpt in excerpts)
        or any(
            item.get("reason")
            in {
                "content_budget",
                "content_budget_exhausted",
                "output_budget",
            }
            for item in omissions
        )
        or suppressed_omissions > 0
    )
    return {
        "schema": 1,
        "route": route,
        "effective_date": effective_date.isoformat(),
        "components": list(components),
        "budget": {
            "limit_bytes": byte_budget,
            "output_bytes": output_bytes,
            "remaining_bytes": max(0, byte_budget - output_bytes),
            "content_bytes": content_bytes,
            "accounting": "entire serialized output in UTF-8",
            "truncated": truncated,
        },
        "excerpts": [
            excerpt_dict(excerpt)
            for excerpt in sorted(
                excerpts,
                key=lambda item: (
                    COMPONENT_RENDER_ORDER[item.candidate.component],
                    item.candidate.ordinal,
                ),
            )
        ],
        "omissions": list(omissions),
        "suppressed_omissions": suppressed_omissions,
    }


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def markdown_scalar(value: object) -> str:
    return str(value).replace("`", "\\`")


def render_markdown(payload: dict[str, Any]) -> str:
    route = payload["route"]
    budget = payload["budget"]
    lines = [
        "## Context Bundle",
        "",
        f"- Intent: `intents.{markdown_scalar(route['name'])}` "
        f"(`{markdown_scalar(route['mode'])}`)",
        f"- Effective date: `{payload['effective_date']}`",
        f"- Components: {', '.join(f'`{item}`' for item in payload['components'])}",
        f"- Byte budget: {budget['output_bytes']} / {budget['limit_bytes']} "
        "UTF-8 bytes (entire output)",
        f"- Remaining: {budget['remaining_bytes']} bytes",
        f"- Truncated: {'yes' if budget['truncated'] else 'no'}",
        "",
    ]

    for excerpt in payload["excerpts"]:
        lines.extend(
            [
                f"### {excerpt['component']}: `{markdown_scalar(excerpt['source'])}`",
                "",
                f"- Section: `{markdown_scalar(excerpt['section'])}`",
                f"- Representation: `{excerpt['representation']}`",
                f"- Bytes: {excerpt['included_bytes']} included; "
                f"{excerpt['available_bytes']} available; "
                f"{excerpt['source_bytes']} in source",
                f"- Truncated: {'yes' if excerpt['truncated'] else 'no'}",
            ]
        )
        if excerpt.get("truncation_reasons"):
            reasons = ", ".join(excerpt["truncation_reasons"])
            lines.append(f"- Truncation reasons: {reasons}")
        lines.extend(
            ["", "<context-excerpt>", excerpt["content"], "</context-excerpt>", ""]
        )

    omissions = payload["omissions"]
    if omissions or payload["suppressed_omissions"]:
        lines.extend(["### Omissions", ""])
        for item in omissions:
            location = item.get("source", "(component)")
            section = (
                f"#{item['section']}" if isinstance(item.get("section"), str) else ""
            )
            lines.append(
                f"- `{item['component']}`: "
                f"`{markdown_scalar(location)}{markdown_scalar(section)}` "
                f"({item['reason']})"
            )
        if payload["suppressed_omissions"]:
            lines.append(
                f"- {payload['suppressed_omissions']} additional omission record(s) "
                "suppressed to fit the byte budget."
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_payload(payload: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return render_json(payload)
    return render_markdown(payload)


def stabilized_render(
    *,
    route: dict[str, Any],
    effective_date: date,
    components: Sequence[str],
    excerpts: Sequence[Excerpt],
    omissions: Sequence[dict[str, Any]],
    byte_budget: int,
    output_format: str,
    suppressed_omissions: int,
) -> tuple[str, int]:
    output_bytes = 0
    rendered = ""
    for _ in range(12):
        payload = payload_dict(
            route=route,
            effective_date=effective_date,
            components=components,
            excerpts=excerpts,
            omissions=omissions,
            byte_budget=byte_budget,
            output_bytes=output_bytes,
            suppressed_omissions=suppressed_omissions,
        )
        rendered = render_payload(payload, output_format)
        measured = utf8_len(rendered)
        if measured == output_bytes:
            return rendered, measured
        output_bytes = measured
    return rendered, utf8_len(rendered)


def fit_rendered_output(
    *,
    route: dict[str, Any],
    effective_date: date,
    components: Sequence[str],
    excerpts: list[Excerpt],
    omissions: list[dict[str, Any]],
    byte_budget: int,
    output_format: str,
) -> str:
    suppressed_omissions = 0
    while True:
        rendered, measured = stabilized_render(
            route=route,
            effective_date=effective_date,
            components=components,
            excerpts=excerpts,
            omissions=omissions,
            byte_budget=byte_budget,
            output_format=output_format,
            suppressed_omissions=suppressed_omissions,
        )
        if measured <= byte_budget:
            return rendered

        overflow = measured - byte_budget
        shrinkable = [
            excerpt
            for excerpt in excerpts
            if excerpt.included_bytes > MIN_EXCERPT_BYTES
        ]
        if shrinkable:
            target = max(
                shrinkable,
                key=lambda item: (
                    item.candidate.priority,
                    item.included_bytes,
                    item.candidate.ordinal,
                ),
            )
            new_limit = max(
                MIN_EXCERPT_BYTES,
                target.included_bytes - overflow - 32,
            )
            target.content = truncate_utf8(target.candidate.text, new_limit)
            target.truncation_reasons.add("output_budget")
            continue

        if excerpts:
            target = max(
                excerpts,
                key=lambda item: (
                    item.candidate.priority,
                    item.included_bytes,
                    item.candidate.ordinal,
                ),
            )
            excerpts.remove(target)
            omissions.append(
                omission(
                    target.candidate.component,
                    target.candidate.source,
                    target.candidate.section,
                    "output_budget",
                )
            )
            continue

        if omissions:
            omissions.pop()
            suppressed_omissions += 1
            continue

        raise BundleError(
            f"--byte-budget {byte_budget} is too small for {output_format} metadata; "
            f"at least {measured} bytes are required"
        )


def build_bundle(
    *,
    vault: Path,
    intents_path: Path,
    intent: str | None,
    components: Sequence[str],
    source_specs: Sequence[str],
    effective_date: date,
    byte_budget: int | None,
    reflection_count: int,
    output_format: str,
) -> str:
    route = resolve_route(intent_arg=intent, intents_path=intents_path)
    selected_budget = (
        byte_budget if byte_budget is not None else route["context_budget_bytes"]
    )
    candidates, omissions = gather_candidates(
        vault=vault,
        route=route,
        components=components,
        source_specs=source_specs,
        effective_date=effective_date,
        reflection_count=reflection_count,
    )
    excerpts = allocate_excerpts(candidates, omissions, selected_budget)
    return fit_rendered_output(
        route=route,
        effective_date=effective_date,
        components=components,
        excerpts=excerpts,
        omissions=omissions,
        byte_budget=selected_budget,
        output_format=output_format,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Emit a deterministic, read-only OV context projection after intent "
            "routing. The byte budget covers the entire UTF-8 output."
        )
    )
    parser.add_argument(
        "--intent",
        required=True,
        help="selected intent registry key, for example 'review' or 'intents.review'",
    )
    parser.add_argument(
        "--vault",
        help="OV vault root (default: $OV)",
    )
    parser.add_argument(
        "--intents",
        default=str(DEFAULT_INTENTS_PATH),
        help="intent registry override (default: harness/intents.toml)",
    )
    parser.add_argument(
        "--component",
        action="append",
        help=(
            "component to include; repeat or comma-separate: profile, session, "
            "reflections, daily, sources. Default excludes daily and sources."
        ),
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="PATH[#SECTION]",
        help=(
            "explicit vault-relative source or Markdown section; repeat as needed. "
            "Explicit sources are included even when 'sources' is not a component."
        ),
    )
    parser.add_argument(
        "--effective-date",
        help="effective date as YYYY-MM-DD (default: local late-sleep rule)",
    )
    parser.add_argument(
        "--byte-budget",
        "--budget",
        type=int,
        default=None,
        help=(
            "maximum serialized UTF-8 output bytes (default: selected "
            "intent's context_budget_bytes; "
            f"maximum: {MAX_BYTE_BUDGET})"
        ),
    )
    parser.add_argument(
        "--reflection-count",
        type=int,
        default=DEFAULT_REFLECTION_COUNT,
        help=(
            f"number of recent dated reflections to project (default: "
            f"{DEFAULT_REFLECTION_COUNT}; maximum: {MAX_REFLECTION_COUNT})"
        ),
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "md"),
        default="json",
        help="output format (default: json; 'md' aliases markdown)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.byte_budget is not None and args.byte_budget <= 0:
            raise BundleError("--byte-budget must be positive")
        if args.byte_budget is not None and args.byte_budget > MAX_BYTE_BUDGET:
            raise BundleError(
                f"--byte-budget exceeds the selected-workflow maximum "
                f"of {MAX_BYTE_BUDGET} bytes"
            )
        if not 0 <= args.reflection_count <= MAX_REFLECTION_COUNT:
            raise BundleError(
                f"--reflection-count must be between 0 and {MAX_REFLECTION_COUNT}"
            )
        components = parse_components(args.component)
        if args.source and "sources" not in components:
            components.append("sources")
        vault = resolve_vault(args.vault)
        intents_path = Path(args.intents).expanduser().resolve()
        effective_date = parse_effective_date(args.effective_date)
        output_format = "markdown" if args.format == "md" else args.format
        rendered = build_bundle(
            vault=vault,
            intents_path=intents_path,
            intent=args.intent,
            components=components,
            source_specs=args.source,
            effective_date=effective_date,
            byte_budget=args.byte_budget,
            reflection_count=args.reflection_count,
            output_format=output_format,
        )
    except BundleError as exc:
        parser.error(str(exc))
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
