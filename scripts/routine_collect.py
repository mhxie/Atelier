"""routine_collect.py: collect routine outputs, updates, health, and context into the digest manifest.

Split out of routine_digest.py; routine_digest.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import sys

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write  # noqa: E402
from routine_digest_core import (  # noqa: E402
    DEFAULT_EXCERPT_CHARS,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_ITEMS,
    MANIFEST_SCHEMA,
    Routine,
    _vault_relative,
    humanize_slug,
    load_acks,
    load_routines,
    source_anchor,
)


OVERVIEW_SCHEMA = 1

BRIEF_SCHEMA = 1  # must match daily_brief.BRIEF_SCHEMA

CONTEXT_SCHEMA = 1  # must match daily_context.CONTEXT_SCHEMA

# Optional private append-only ledgers whose new rows must appear in digests.
# Paths and labels stay under $OV so the public harness never names a private
# tracker. Daily delivery uses its own cursor: routine_acks.json means
# "reviewed", while this state means only "written into a digest artifact".
DIGEST_UPDATES_CONFIG = "_meta/digest_updates.toml"

DIGEST_UPDATES_STATE = "_meta/digest_update_state.json"

# Research first. The fleet writes fourteen finance files for every research
# one, and the curated depth is picked from what the manifest shows first, so
# lane order is the cheapest lever on which lane the reader's minutes go to.
# `deep_read_lane_gap` is the second lever: it names the miss when the pick
# still skips research on a window that had it.
LANE_ORDER = ["Research", "Tech feed", "Finance", "Toolcraft", "Career", "Findings"]

_DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)

_FENCED_BLOCK = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*$", re.DOTALL | re.MULTILINE)

_META_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*:\s")

_SOURCE_URL = re.compile(r"^\s*source_url:\s*(\S+)\s*$", re.MULTILINE)

_LIST_LINK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+.*?\[([^\]]+)\]\((https?://[^\s)]+)\)")

_TABLE_LINK = re.compile(r"^\s*\|.*?\[([^\]]+)\]\((https?://[^\s)]+)\)")

_BARE_TABLE_URL = re.compile(r"^\s*\|.*?(https?://[^\s|)]+)")

_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

_ANY_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Section titles that carry the analytical payload, preferred for the excerpt.
_PAYLOAD_HEADINGS = (
    "why this matters",
    "why now",
    "assessment",
    "implication",
    "implications",
    "so what",
    "takeaway",
    "takeaways",
    "summary",
    "verdict",
    "decision",
    "conclusion",
    "结论",
    "判断",
    "影响",
)

_ANY_MD_LINK = re.compile(r"\[([^\]]+)\]\(\s*<?[^)]*>?\s*\)")

# Headings that name bookkeeping rather than the finding, so they make a poor
# document headline even though the section body is often the best excerpt.
_GENERIC_HEADINGS = {
    "tl;dr",
    "tldr",
    "collection status",
    "collection notes",
    "coverage",
    "facts",
    "status",
    "notes",
    "summary",
    "overview",
    "scope",
}

UNIT_EXCERPT_CHARS = 320

MAX_UNITS_PER_FILE = 8

@dataclass
class DigestUpdateSource:
    name: str
    label: str
    path: str
    section: str
    date_column: str
    display_columns: list[str]
    since: date | None = None

def load_update_sources(ov: Path) -> tuple[list[DigestUpdateSource], list[str]]:
    """Load optional private ledger declarations.

    The configuration is deliberately data-only. A private vault can name any
    append-only Markdown table without adding its filename or subject to the
    public harness.
    """
    config_path = ov / DIGEST_UPDATES_CONFIG
    if not config_path.is_file():
        return [], []
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [], [f"digest update config unreadable: {exc!r}"]

    sources: list[DigestUpdateSource] = []
    warnings: list[str] = []
    seen_names: set[str] = set()
    for index, row in enumerate(config.get("source", []), start=1):
        if not isinstance(row, dict):
            warnings.append(f"digest update source #{index} is not a table")
            continue
        required = ("name", "label", "path", "section", "date_column")
        missing = [key for key in required if not row.get(key)]
        columns = row.get("display_columns")
        if missing or not isinstance(columns, list) or not all(
            isinstance(value, str) and value for value in columns
        ):
            detail = f"missing {', '.join(missing)}" if missing else "invalid display_columns"
            warnings.append(f"digest update source #{index}: {detail}")
            continue

        name = str(row["name"])
        if name in seen_names:
            warnings.append(f"digest update source {name!r} is duplicated")
            continue
        seen_names.add(name)

        relative = Path(str(row["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            warnings.append(f"digest update source {name!r} path must stay under $OV")
            continue

        since_value: date | None = None
        if row.get("since"):
            try:
                since_value = date.fromisoformat(str(row["since"]))
            except ValueError:
                warnings.append(f"digest update source {name!r} has invalid since date")
                continue
        sources.append(
            DigestUpdateSource(
                name=name,
                label=str(row["label"]),
                path=str(relative),
                section=str(row["section"]),
                date_column=str(row["date_column"]),
                display_columns=[str(value) for value in columns],
                since=since_value,
            )
        )
    return sources, warnings

def load_update_state(ov: Path) -> tuple[dict[str, str], list[str]]:
    path = ov / DIGEST_UPDATES_STATE
    if not path.is_file():
        return {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {}, [f"digest update state unreadable: {exc!r}"]
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        return {}, ["digest update state has unsupported schema"]
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return {}, ["digest update state daily cursor is invalid"]
    return {str(k): str(v) for k, v in daily.items() if isinstance(v, str)}, []

def _markdown_cells(line: str) -> list[str]:
    raw = line.strip()
    if not raw.startswith("|"):
        return []
    return [cell.strip() for cell in raw.strip("|").split("|")]

def _markdown_table(text: str, section: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    heading = re.compile(rf"^#{{1,6}}\s+{re.escape(section)}\s*$", re.MULTILINE)
    match = heading.search(text)
    if not match:
        return [], []
    lines = text[match.end():].splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    if start is None or start + 1 >= len(lines):
        return [], []
    headers = _markdown_cells(lines[start])
    separator = _markdown_cells(lines[start + 1])
    if not headers or len(separator) != len(headers) or not all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator
    ):
        return [], []
    rows: list[tuple[str, list[str]]] = []
    for raw in lines[start + 2:]:
        if not raw.lstrip().startswith("|"):
            break
        cells = _markdown_cells(raw)
        if len(cells) != len(headers):
            continue
        rows.append((raw.strip(), cells))
    return headers, rows

def collect_digest_updates(
    ov: Path,
    *,
    mode: str,
    start: date,
    end: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Select configured ledger rows for deterministic digest rendering.

    Daily mode is cursor-based so an update made after the morning report lands
    in the next report exactly once. Weekly mode is window-based so the same
    update is repeated once in that week's roll-up, as a weekly report should.
    """
    sources, warnings = load_update_sources(ov)
    daily_state, state_warnings = load_update_state(ov)
    warnings.extend(state_warnings)
    selected: list[dict[str, Any]] = []

    for source in sources:
        path = ov / source.path
        if not path.is_file():
            warnings.append(f"digest update source {source.name!r} missing: {source.path}")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warnings.append(f"digest update source {source.name!r} unreadable: {exc!r}")
            continue
        headers, raw_rows = _markdown_table(text, source.section)
        if not headers:
            warnings.append(
                f"digest update source {source.name!r} has no table under {source.section!r}"
            )
            continue
        required_columns = [source.date_column, *source.display_columns]
        missing = [column for column in required_columns if column not in headers]
        if missing:
            warnings.append(
                f"digest update source {source.name!r} missing columns: {', '.join(missing)}"
            )
            continue

        parsed: list[dict[str, Any]] = []
        for sequence, (raw, cells) in enumerate(raw_rows):
            values = dict(zip(headers, cells))
            try:
                checked = date.fromisoformat(values[source.date_column])
            except ValueError:
                warnings.append(
                    f"digest update source {source.name!r} has invalid "
                    f"{source.date_column}: {values[source.date_column]!r}"
                )
                continue
            if source.since and checked < source.since:
                continue
            row_id = hashlib.sha256(f"{source.name}\0{raw}".encode("utf-8")).hexdigest()
            parsed.append(
                {
                    "id": row_id,
                    "source": source.name,
                    "label": source.label,
                    "path": source.path,
                    "section": source.section,
                    "date": checked.isoformat(),
                    "sequence": sequence,
                    "values": {column: values[column] for column in source.display_columns},
                }
            )

        if mode == "daily":
            candidates = parsed
            cursor = daily_state.get(source.name)
            if cursor:
                cursor_index = next(
                    (index for index, item in enumerate(parsed) if item["id"] == cursor), None
                )
                if cursor_index is None:
                    warnings.append(
                        f"digest update cursor for {source.name!r} no longer matches; "
                        "replaying configured rows"
                    )
                else:
                    candidates = parsed[cursor_index + 1:]
            # A backdated daily render must never pull a future ledger row.
            # There is intentionally no lower window bound: an unreported
            # late-day update belongs in the next artifact, even on the next date.
            selected.extend(
                item for item in candidates if date.fromisoformat(item["date"]) <= end
            )
        else:
            selected.extend(
                item for item in parsed if start <= date.fromisoformat(item["date"]) <= end
            )

    selected.sort(
        key=lambda item: (
            item["date"],
            item["label"],
            item["source"],
            item["sequence"],
        )
    )
    return selected, warnings

def advance_update_state(ov: Path, manifest: dict[str, Any]) -> None:
    """Mark daily updates as written, without claiming they were reviewed."""
    if manifest.get("mode") != "daily" or not manifest.get("updates"):
        return
    current, _ = load_update_state(ov)
    for item in manifest["updates"]:
        current[str(item["source"])] = str(item["id"])
    payload = {"schema": 1, "daily": dict(sorted(current.items()))}
    atomic_write(
        ov / DIGEST_UPDATES_STATE,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )

def effective_date(now: datetime | None = None) -> date:
    """Today, or yesterday before 03:00 local -- the harness day boundary."""
    now = now or datetime.now()
    return now.date() - timedelta(days=1) if now.hour < 3 else now.date()

def resolve_window(
    mode: str,
    *,
    days: int | None,
    since: str | None,
    until: str | None,
    now: datetime | None = None,
) -> tuple[date, date, int]:
    end = _parse_date(until, "until") if until else effective_date(now)
    if since:
        start = _parse_date(since, "since")
        if start > end:
            raise SystemExit(f"--since {start} is after --until {end}")
        return start, end, (end - start).days + 1
    span = days if days is not None else (1 if mode == "daily" else 7)
    if span < 1:
        raise SystemExit(f"--days must be >= 1, got {span}")
    return end - timedelta(days=span - 1), end, span

def _parse_date(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"--{label} must be YYYY-MM-DD, got {value!r}") from exc

def file_date(path: Path) -> tuple[date, str]:
    """Date of a routine output, from its filename when possible.

    Filenames carry the date in varying positions across routines: leading
    (`2099-01-02-<slug>.md`), trailing after a hyphen (`<slug>-2099-01-02.md`),
    and trailing after an underscore (`<slug>_2099-01-02.md`). So the first ISO
    date anywhere in the stem wins. mtime is the fallback and is reported as
    such, because a re-synced vault rewrites mtimes.
    """
    match = _DATE_IN_NAME.search(path.name)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))), "filename"
        except ValueError:
            pass
    return date.fromtimestamp(path.stat().st_mtime), "mtime"

def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading YAML frontmatter block off the body.

    Deliberately a flat `key: value` reader, not a YAML parser: routine
    frontmatter is machine-written and flat, and a dependency is not worth it.
    Values keep their raw form (including `[A, B]` lists) as strings.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    return _parse_meta_lines(match.group(1)), text[match.end():]

def _looks_like_meta_block(raw: str) -> bool:
    """True when a `---` fenced block is frontmatter, not a horizontal rule.

    Every non-empty line must read as `key: value`. Without this guard a
    markdown `---` divider pair would be parsed as metadata.
    """
    lines = [line for line in raw.splitlines() if line.strip()]
    return bool(lines) and all(_META_LINE.match(line) for line in lines)

def split_units(text: str) -> list[tuple[dict[str, str], str]]:
    """Split a multi-signal report into its embedded units.

    Collector routines pack several independent findings into one dated file,
    each introduced by its own frontmatter block carrying a `slug`. A single
    file-level headline and excerpt would throw most of that away, so each
    slug-bearing block becomes its own unit, with the prose up to the next
    block as its body. Blocks without a `slug` are document metadata (the tech
    digest's leading header), not units.
    """
    units: list[tuple[dict[str, str], str]] = []
    blocks = [m for m in _FENCED_BLOCK.finditer(text) if _looks_like_meta_block(m.group(1))]
    for index, match in enumerate(blocks):
        meta = _parse_meta_lines(match.group(1))
        if "slug" not in meta:
            continue
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(text)
        units.append((meta, text[match.end():end]))
        if len(units) >= MAX_UNITS_PER_FILE:
            break
    return units

def _parse_meta_lines(raw: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        meta[key.strip()] = value.strip().strip("\"'")
    return meta

def extract_headline(
    meta: dict[str, str], body: str, units: list[tuple[dict[str, str], str]]
) -> str:
    """Best one-line name for a routine output.

    An H1 always wins. Otherwise a multi-signal file names itself by its slugs,
    and a single-signal file falls through to the first heading that is not
    collection bookkeeping. Returning "" is allowed: the source index already
    shows the routine label and date, so a fabricated headline is worse than none.
    """
    h1 = _H1.search(body)
    if h1:
        return h1.group(1).strip()
    if units:
        slugs = [humanize_slug(m.get("slug", "")) for m, _ in units if m.get("slug")]
        if slugs:
            shown = ", ".join(slugs[:3])
            extra = f", +{len(slugs) - 3}" if len(slugs) > 3 else ""
            return f"{len(slugs)} signals: {shown}{extra}"
    for match in _ANY_HEADING.finditer(body):
        title = match.group(2).strip()
        if title.lower().rstrip(":").strip() not in _GENERIC_HEADINGS:
            return title
    for key in ("slug", "title", "type"):
        if meta.get(key):
            return humanize_slug(meta[key])
    first = _ANY_HEADING.search(body)
    return first.group(2).strip() if first else ""

_ITEM_META_TAIL = re.compile(r"\*\*(?:Why now|Provenance)[^*]*\*\*.*$", re.I)

_BOLD_RUN = re.compile(r"\*\*([^*]+)\*\*")

def _item_note(lines: list[str], index: int) -> str:
    """The gloss line under a feed item, if the report wrote one.

    Feed-shaped routine reports put the link on one line and a sentence of
    context on the next, indented. That sentence is what makes an item readable
    without opening it, and it was being discarded: the manifest kept only the
    title and URL, so a news section could offer a headline and nothing else.

    The trailing classification (`**Why now:** ... **Provenance:** ...`) is
    bookkeeping for whoever tunes the routine, not for the reader, so it is cut.
    """
    parts: list[str] = []
    for line in lines[index + 1 : index + 4]:
        if not line.strip() or not line.startswith((" ", "\t")):
            break
        parts.append(line.strip())
    note = " ".join(parts)
    note = _ITEM_META_TAIL.sub("", note)
    note = _BOLD_RUN.sub(r"\1", note)
    note = strip_inline_markup(note)
    return re.sub(r"\s+", " ", note).strip(" ·-—,;")

def extract_items(body: str, cap: int) -> list[dict[str, str]]:
    """Titled links from list items and table rows, deduped by URL.

    Feed-shaped routine outputs (a news digest) carry their payload as
    a numbered list of `[title](url)`; table-shaped ones (a tool scout) carry
    it in table cells. Prose links are ignored on purpose: they are citations
    inside an argument, not enumerable items.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    lines = body.splitlines()
    for position, line in enumerate(lines):
        if len(items) >= cap:
            break
        match = _LIST_LINK.match(line) or _TABLE_LINK.match(line)
        if match:
            title, url = match.group(1).strip(), match.group(2)
        else:
            bare = _BARE_TABLE_URL.match(line)
            if not bare:
                continue
            url = bare.group(1)
            title = ""
        url = url.rstrip(").,;")
        if url in seen:
            continue
        seen.add(url)
        item = {"title": title, "url": url}
        note = _item_note(lines, position)
        if note:
            item["note"] = note
        items.append(item)
    return items

def extract_excerpt(body: str, limit: int) -> str:
    """Bounded prose projection of one routine output.

    Prefers an analytical section ("Why This Matters" and friends) over the
    opening lines, because the opening of a collector report is usually
    coverage bookkeeping. Tables, headings, and frontmatter fences are dropped:
    they do not read well truncated.
    """
    if limit <= 0:
        return ""
    sections = _split_sections(body)
    chosen: list[str] = []
    for title, content in sections:
        if any(marker in title.lower() for marker in _PAYLOAD_HEADINGS):
            chosen = _prose_lines(content)
            if chosen:
                break
    if not chosen:
        for _, content in sections:
            chosen = _prose_lines(content)
            if chosen:
                break
    if not chosen:
        chosen = _prose_lines(body)
    text = strip_inline_markup(" ".join(chosen))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("。"), cut.rfind("; "))
    if boundary > limit * 0.6:
        cut = cut[: boundary + 1]
    return cut.rstrip() + " …"

def strip_inline_markup(text: str) -> str:
    """Flatten markdown emphasis and links into plain prose.

    Excerpts are HTML-escaped at render time rather than converted, so leaving
    `**bold**` in them would print the asterisks. Link text is kept and the URL
    dropped: the index already carries the real links.
    """
    text = re.sub(r"\[\^[^\]]*\]", "", text)
    text = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text)
    # Any markdown link, not just http: vault-relative targets appear as
    # `[Title](<../finance/Some Tracker.md>)`, and half a truncated one reads
    # worse than no link at all.
    text = _ANY_MD_LINK.sub(r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text

def _split_sections(body: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    matches = list(_ANY_HEADING.finditer(body))
    if not matches:
        return [("", body)]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((match.group(2), body[match.end():end]))
    return sections

def _prose_lines(chunk: str) -> list[str]:
    out: list[str] = []
    in_fence = False
    for raw in chunk.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        if line.startswith(("|", "#", "---", "===", ">")):
            continue
        if re.match(r"^[a-z_]+:\s", line):  # stray frontmatter key
            continue
        out.append(re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line))
        if sum(len(part) for part in out) > 2000:
            break
    return out

@dataclass
class Source:
    routine: str
    label: str
    lane: str
    max_lines: int
    path: str
    name: str
    date: str
    date_source: str
    bytes: int
    headline: str
    meta: dict[str, str]
    excerpt: str
    items: list[dict[str, str]]
    primary_urls: list[str] = field(default_factory=list)
    units: list[dict[str, Any]] = field(default_factory=list)

def collect(
    ov: Path,
    *,
    mode: str,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    unacked: bool = False,
    include_maintenance: bool = False,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_files: int = DEFAULT_MAX_FILES,
    now: datetime | None = None,
) -> dict[str, Any]:
    routines = load_routines(ov)
    acks = load_acks(ov)
    start, end, span = resolve_window(mode, days=days, since=since, until=until, now=now)

    sources: list[Source] = []
    skipped: list[str] = []
    truncated = False
    active: list[Routine] = []
    for routine in routines:
        if not routine.include and not include_maintenance:
            skipped.append(routine.label)
        else:
            active.append(routine)

    if unacked:
        # Acks are a per-directory high-water mark on the filename, and several
        # routines can share one output_dir. Selecting per routine would let a
        # batch of routine A's oldest files advance the mark past routine B's
        # older, never-shown files. So the unit here is the directory: every
        # active routine's files in it, oldest name first, which makes the
        # mark after an ack exactly the last file the reader saw.
        by_dir: dict[str, list[Routine]] = {}
        for routine in active:
            by_dir.setdefault(routine.output_dir, []).append(routine)
        for output_dir, members in by_dir.items():
            directory = ov / output_dir
            if not directory.is_dir():
                continue
            ack = acks.get(output_dir, "")
            candidates: dict[str, tuple[Path, Routine]] = {}
            for routine in members:
                for path in directory.glob(routine.file_pattern):
                    if path.name > ack:
                        candidates.setdefault(path.name, (path, routine))
            for name in sorted(candidates):
                if len(sources) >= max_files:
                    truncated = True
                    break
                path, routine = candidates[name]
                when, when_source = file_date(path)
                sources.append(
                    _build_source(ov, path, routine, when, when_source, excerpt_chars, max_items)
                )
            if truncated:
                break
    else:
        for routine in active:
            directory = ov / routine.output_dir
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob(routine.file_pattern), key=lambda p: p.name):
                when, when_source = file_date(path)
                if not (start <= when <= end):
                    continue
                if len(sources) >= max_files:
                    truncated = True
                    break
                sources.append(
                    _build_source(ov, path, routine, when, when_source, excerpt_chars, max_items)
                )
            if truncated:
                break

    sources.sort(key=lambda s: (s.date, s.label, s.name), reverse=True)
    lanes = _group_lanes(sources)
    updates, update_warnings = collect_digest_updates(
        ov,
        mode=mode,
        start=start,
        end=end,
    )
    ack_targets: dict[str, str] = {}
    for source in sources:
        directory = str(Path(source.path).parent)
        if source.name > ack_targets.get(directory, ""):
            ack_targets[directory] = source.name

    return {
        "schema": MANIFEST_SCHEMA,
        "mode": mode,
        "selection": "unacked" if unacked else "window",
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": {"since": start.isoformat(), "until": end.isoformat(), "days": span},
        "counts": {
            "routines": len({s.routine for s in sources}),
            "files": len(sources),
            "updates": len(updates),
            "bytes": sum(s.bytes for s in sources),
            "lanes": len(lanes),
        },
        "truncated": truncated,
        "health": collect_health(ov, routines, acks, start, end),
        "skipped_routines": skipped,
        "lanes": lanes,
        "updates": updates,
        "update_warnings": update_warnings,
        "acks": ack_targets,
    }

def collect_health(
    ov: Path,
    routines: list[Routine],
    acks: dict[str, str],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Fleet numbers for the window: who reported, who failed, what is owed.

    The digest was answering "what did the routines say" without ever answering
    "did the routines run". Those come apart badly: a thin window reads as a
    quiet day when it is actually a broken scheduler. Measured on 2026-08-31,
    95 of 279 claims were failures, and nothing in the document said so.

    Counts only, and only from files already on disk. Anything that needs a
    judgement stays in the overview where a human wrote it.
    """
    included = [r for r in routines if r.include]
    reported: set[str] = set()
    for routine in included:
        directory = ov / routine.output_dir
        if not directory.is_dir():
            continue
        for path in directory.glob(routine.file_pattern):
            when, _ = file_date(path)
            if start <= when <= end:
                reported.add(routine.name)
                break

    completed = failed = other = 0
    runs_root = ov / "_meta" / "routine_runs"
    for routine in included:
        directory = runs_root / routine.name
        if not directory.is_dir():
            continue
        for path in directory.glob("*.toml"):
            try:
                cycle = date.fromisoformat(path.stem[:10])
            except ValueError:
                continue
            if not (start <= cycle <= end):
                continue
            try:
                claim = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            status = str(claim.get("status") or "")
            if status == "completed":
                completed += 1
            elif status in {"failed", "completion-uncertain"}:
                failed += 1
            else:
                other += 1

    # Review debt is the backlog this digest exists to drain, so it belongs on
    # the face of the document rather than in a session-start cue nobody reads
    # on a phone.
    debt = 0
    for routine in included:
        directory = ov / routine.output_dir
        if not directory.is_dir():
            continue
        ack = acks.get(routine.output_dir, "")
        debt += sum(1 for p in directory.glob(routine.file_pattern) if p.name > ack)

    return {
        "declared": len(included),
        "reported": len(reported),
        "completed": completed,
        "failed": failed,
        "running_or_deferred": other,
        "review_debt": debt,
    }

def _build_source(
    ov: Path,
    path: Path,
    routine: Routine,
    when: date,
    when_source: str,
    excerpt_chars: int,
    max_items: int,
) -> Source:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    meta, body = parse_frontmatter(text)
    units = split_units(text)
    urls = []
    seen: set[str] = set()
    for match in _SOURCE_URL.finditer(text):
        url = match.group(1).strip().strip("\"'")
        if url.startswith("http") and url not in seen:
            seen.add(url)
            urls.append(url)
    keep = {"date", "type", "slug", "signal_type", "source_type", "source_tier", "status", "item_count", "window"}
    # A multi-signal file's substance lives in its units; a file-level excerpt
    # on top of them would just repeat the first unit.
    file_excerpt = "" if units else extract_excerpt(body, excerpt_chars)
    return Source(
        routine=routine.name,
        label=routine.label,
        lane=routine.lane,
        max_lines=routine.max_lines,
        path=_vault_relative(ov, path),
        name=path.name,
        date=when.isoformat(),
        date_source=when_source,
        bytes=len(text.encode("utf-8")),
        headline=extract_headline(meta, body, units),
        meta={k: v for k, v in meta.items() if k in keep},
        excerpt=file_excerpt,
        items=extract_items(body, max_items),
        primary_urls=urls[:10],
        units=[_unit_dict(unit_meta, unit_body) for unit_meta, unit_body in units],
    )

def _unit_dict(meta: dict[str, str], body: str) -> dict[str, Any]:
    unit: dict[str, Any] = {"slug": meta.get("slug", "")}
    for key in ("signal_type", "source_type", "source_tier", "date"):
        if meta.get(key):
            unit[key] = meta[key]
    if meta.get("source_url", "").startswith("http"):
        unit["source_url"] = meta["source_url"]
    excerpt = extract_excerpt(body, UNIT_EXCERPT_CHARS)
    if excerpt:
        unit["excerpt"] = excerpt
    return unit

def _group_lanes(sources: list[Source]) -> list[dict[str, Any]]:
    buckets: dict[str, list[Source]] = {}
    for source in sources:
        buckets.setdefault(source.lane, []).append(source)

    def lane_key(lane: str) -> tuple[int, str]:
        return (LANE_ORDER.index(lane) if lane in LANE_ORDER else len(LANE_ORDER), lane)

    return [
        {
            "lane": lane,
            "files": len(buckets[lane]),
            "sources": [_source_dict(s) for s in buckets[lane]],
        }
        for lane in sorted(buckets, key=lane_key)
    ]

def _source_dict(source: Source) -> dict[str, Any]:
    data = {
        "routine": source.routine,
        "label": source.label,
        "max_lines": source.max_lines,
        "path": source.path,
        "name": source.name,
        "date": source.date,
        "bytes": source.bytes,
        "headline": source.headline,
        "anchor": source_anchor(source.path),
    }
    if source.date_source != "filename":
        data["date_source"] = source.date_source
    if source.meta:
        data["meta"] = source.meta
    if source.excerpt:
        data["excerpt"] = source.excerpt
    if source.units:
        data["units"] = source.units
    if source.items:
        data["items"] = source.items
    if source.primary_urls:
        data["primary_urls"] = source.primary_urls
    return data

def load_overview(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"overview unreadable: {exc!r}") from exc
    if not isinstance(data, dict):
        raise SystemExit("overview must be a JSON object")
    schema = data.get("schema", OVERVIEW_SCHEMA)
    if schema != OVERVIEW_SCHEMA:
        raise SystemExit(f"overview schema {schema} unsupported (expected {OVERVIEW_SCHEMA})")
    return data

def load_context(path: Path | None) -> dict[str, Any]:
    """Masthead context from daily_context.py: weather and harness quota."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"context unreadable: {exc!r}") from exc
    if not isinstance(data, dict):
        raise SystemExit("context must be a JSON object")
    schema = data.get("schema", CONTEXT_SCHEMA)
    if schema != CONTEXT_SCHEMA:
        raise SystemExit(f"context schema {schema} unsupported (expected {CONTEXT_SCHEMA})")
    return data

def load_retrospect(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"retrospect picks unreadable: {exc!r}") from exc
    picks = data.get("picks") if isinstance(data, dict) else data
    return [p for p in picks or [] if isinstance(p, dict)]

def load_brief(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"brief unreadable: {exc!r}") from exc
    if not isinstance(data, dict):
        raise SystemExit("brief must be a JSON object")
    schema = data.get("schema", BRIEF_SCHEMA)
    if schema != BRIEF_SCHEMA:
        raise SystemExit(f"brief schema {schema} unsupported (expected {BRIEF_SCHEMA})")
    return data
