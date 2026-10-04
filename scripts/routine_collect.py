"""routine_collect.py: collect routine outputs, updates, health, and context into the digest manifest.
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
from _paths import date_in_text  # noqa: E402
from routine_digest_core import (  # noqa: E402
    DEFAULT_EXCERPT_CHARS,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_ITEMS,
    MANIFEST_SCHEMA,
    Routine,
    _vault_relative,
    humanize_slug,
    iter_sources,
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
TODO_REMINDER_LIMIT = 3
# Daily selection reaches back this many days for files no earlier daily
# digest delivered. A routine that finishes after the morning run writes a
# file dated today, and a strict one-day window tomorrow would never see it:
# the Thursday finance routines run at 07:00 and 08:00, the digest at 06:00,
# so their output was never digested at all. Carried files are marked in the
# manifest and the delivered ledger stops them from repeating the day after.
DAILY_CARRY_DAYS = 1
# Delivered paths are kept this long so the ledger does not grow forever.
DELIVERED_RETENTION_DAYS = 14

# Research first. The fleet writes fourteen finance files for every research
# one, and the curated depth is picked from what the manifest shows first, so
# lane order is the cheapest lever on which lane the reader's minutes go to.
# `deep_read_lane_gap` is the second lever: it names the miss when the pick
# still skips research on a window that had it.
LANE_ORDER = ["Research", "Tech feed", "Finance", "Toolcraft", "Career", "Findings"]


_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)

# A signal unit's metadata sits in a `---` block or, so Reflect hides it, an own-line comment.
_FENCED_BLOCK = re.compile(r"^(?:(<!--)|---)[ \t]*\n(?P<body>.*?)\n(?(1)-->|---)[ \t]*$", re.DOTALL | re.MULTILINE)

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
    if not config_path.exists() and not config_path.is_symlink():
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

def _load_state_payload(ov: Path) -> tuple[dict[str, Any], list[str]]:
    path = ov / DIGEST_UPDATES_STATE
    if not path.is_file():
        return {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {}, [f"digest update state unreadable: {exc!r}"]
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        return {}, ["digest update state has unsupported schema"]
    return payload, []

def load_todo_reminders(ov: Path) -> dict[str, list[str]]:
    payload, warnings = _load_state_payload(ov)
    reminders = payload.get("todo_reminders", {})
    if warnings or not isinstance(reminders, dict) or any(
        not isinstance(days, list) or any(not isinstance(day, str) for day in days)
        for days in reminders.values()
    ):
        raise SystemExit("; ".join(warnings) or "invalid TODO reminder state")
    return reminders


def load_update_state(ov: Path) -> tuple[dict[str, str], list[str]]:
    payload, warnings = _load_state_payload(ov)
    if warnings or not payload:
        return {}, warnings
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return {}, ["digest update state daily cursor is invalid"]
    return {str(k): str(v) for k, v in daily.items() if isinstance(v, str)}, []

def load_delivered_state(ov: Path) -> tuple[dict[str, str], list[str]]:
    """{vault-relative path: effective date of the daily digest that carried it}.

    Written by `write` for daily artifacts only. A path is skipped by a later
    day's carry-back when its recorded date is earlier than that day, so a
    same-day re-run reproduces the same selection and the next day drops it.
    """
    payload, warnings = _load_state_payload(ov)
    if warnings or not payload:
        return {}, warnings
    delivered = payload.get("delivered", {})
    if not isinstance(delivered, dict):
        return {}, ["digest update state delivered ledger is invalid"]
    return {str(k): str(v) for k, v in delivered.items() if isinstance(v, str)}, []

def row_key(source_name: str, headers: list[str], cells: list[str]) -> str:
    """Identity of one ledger row, from its normalized values.

    Persisted as the daily delivery cursor, so it must not move when the table
    is only reformatted. Column order is part of the key; pipe alignment, cell
    padding, and internal whitespace runs are not.
    """
    normalized = "\x1f".join(
        f"{header.strip()}={' '.join(cell.split())}"
        for header, cell in zip(headers, cells)
    )
    return hashlib.sha256(f"{source_name}\0{normalized}".encode("utf-8")).hexdigest()

def legacy_row_id(source_name: str, raw_line: str) -> str:
    """Pre-normalization identity: a hash of the raw source line.

    Only read, never written. A cursor stored before `row_key` existed still
    resolves through this, so no state migration is needed; the next write
    replaces it with the normalized key.
    """
    return hashlib.sha256(f"{source_name}\0{raw_line}".encode("utf-8")).hexdigest()

def _markdown_cells(line: str) -> list[str]:
    raw = line.strip()
    if not raw.startswith("|"):
        return []
    return [cell.strip() for cell in raw.strip("|").split("|")]

def _markdown_table(
    text: str, section: str
) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    heading = re.compile(rf"^#{{1,6}}\s+{re.escape(section)}\s*$", re.MULTILINE)
    match = heading.search(text)
    if not match:
        return [], [], []
    lines = text[match.end():].splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    if start is None or start + 1 >= len(lines):
        return [], [], []
    headers = _markdown_cells(lines[start])
    separator = _markdown_cells(lines[start + 1])
    if not headers or len(separator) != len(headers) or not all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator
    ):
        return [], [], []
    rows: list[tuple[str, list[str]]] = []
    rejected: list[str] = []
    for raw in lines[start + 2:]:
        if not raw.lstrip().startswith("|"):
            break
        cells = _markdown_cells(raw)
        if len(cells) != len(headers):
            rejected.append(raw.strip())
            continue
        rows.append((raw.strip(), cells))
    return headers, rows, rejected

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
        headers, raw_rows, rejected = _markdown_table(text, source.section)
        if not headers:
            warnings.append(
                f"digest update source {source.name!r} has no table under {source.section!r}"
            )
            continue
        if rejected:
            warnings.append(
                f"digest update source {source.name!r} skipped {len(rejected)} row(s) "
                f"whose cell count disagrees with the header: {rejected[0]!r}"
            )
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
            parsed.append(
                {
                    "id": row_key(source.name, headers, cells),
                    "legacy_id": legacy_row_id(source.name, raw),
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
                    (
                        index
                        for index, item in enumerate(parsed)
                        if cursor in (item["id"], item["legacy_id"])
                    ),
                    None,
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
            for item in candidates:
                if date.fromisoformat(item["date"]) > end:
                    break  # An append cursor cannot skip an undelivered row.
                selected.append(item)
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

def prepare_update_state(ov: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Merge delivery state under the publisher's lock without moving cursors back."""
    if manifest.get("mode") != "daily":
        return None
    delivered_on = str(manifest.get("window", {}).get("until", ""))[:10]
    current, warnings = load_update_state(ov)
    delivered, more = load_delivered_state(ov)
    if warnings or more:
        raise SystemExit("; ".join(warnings + more))
    updates = manifest.get("updates") or []
    positions: dict[tuple[str, str], int] = {}
    declarations, warnings = load_update_sources(ov)
    if warnings:
        raise SystemExit("; ".join(warnings))
    configured = {source.name for source in declarations}
    if updates:
        rows, more = collect_digest_updates(ov, mode="weekly", start=date.min, end=date.max)
        if more:
            raise SystemExit("; ".join(more))
        for row in rows:
            for key in ("id", "legacy_id"):
                positions.setdefault((row["source"], row[key]), row["sequence"])
    candidates = {}
    for item in sorted(updates, key=lambda row: positions.get((row["source"], row["id"]), row.get("sequence", 0))):
        candidates[str(item["source"])] = str(item["id"])
    for source, candidate in candidates.items():
        proposed = positions.get((source, candidate))
        previous = positions.get((source, current.get(source, "")))
        if source in configured and proposed is None:
            raise SystemExit(f"digest update for {source!r} no longer resolves; recollect before writing")
        if source in current and current[source] != candidate:
            if proposed is None or previous is None:
                raise SystemExit(f"cannot order digest updates for {source!r}; repair the source before writing")
            if proposed < previous:
                continue
        current[source] = candidate
    if delivered_on:
        for _lane, source in iter_sources(manifest):
            path = str(source.get("path", ""))
            if path:
                delivered.setdefault(path, delivered_on)
        try:
            floor = date.fromisoformat(delivered_on) - timedelta(days=DELIVERED_RETENTION_DAYS)
            delivered = {k: v for k, v in delivered.items() if v[:10] >= floor.isoformat()}
        except ValueError:
            pass
    reminders = load_todo_reminders(ov)
    if not current and not delivered and not reminders:
        return None
    return {
        "schema": 1,
        "daily": dict(sorted(current.items())),
        "delivered": dict(sorted(delivered.items())),
        "todo_reminders": reminders,
    }

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
    when = date_in_text(path.name)
    if when is not None:
        return when, "filename"
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

def strip_frontmatter(text: str) -> str:
    """The body without a leading metadata block; a leading `---` rule stays.

    For renderers that show a routine file in full: the fence and its
    `key: value` lines are bookkeeping and read as noise in a mail client.
    """
    match = _FRONTMATTER.match(text)
    if not match or not _looks_like_meta_block(match.group(1)):
        return text
    return text[match.end():]

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
    blocks = [m for m in _FENCED_BLOCK.finditer(text) if _looks_like_meta_block(m["body"])]
    for index, match in enumerate(blocks):
        meta = _parse_meta_lines(match["body"])
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

# The classification tail a feed routine appends to each item, in every shape
# it has been observed writing it: bold keys, a bare "Why now:", a backtick
# cluster chip, or plain "Cluster:" on its own bullet. The earliest match cuts.
_META_TAILS = (
    re.compile(r"\*\*\s*(?:Cluster|Tag|Mode|Why now|Provenance)\b", re.I),
    re.compile(r"\bWhy now\s*[:：]", re.I),
    re.compile(r"`[^`]+`\s*·\s*\*\*"),
    re.compile(r"(?:^|[\s|·-])(?:Cluster|Provenance)\s*[:：]", re.I),
)

_BOLD_RUN = re.compile(r"\*\*([^*]+)\*\*")

# Separators a routine puts between the link and an inline summary. A plain
# hyphen counts only when whitespace follows it: `-5%` is a sign, not a dash.
_INLINE_NOTE_LEAD = re.compile(r"^\s*(?:(?:[—–:：·]|-(?=\s))\s*)+")
_NOTE_TRAIL = re.compile(r"[\s·\-—,;|]+$")
_NOTE_LEAD = re.compile(r"^(?:(?:[·—,;|]|-(?=\s))\s*)+")

def _strip_meta_tail(note: str) -> str:
    cut = min((m.start() for rx in _META_TAILS if (m := rx.search(note))), default=None)
    return note if cut is None else note[:cut]

def _item_note(lines: list[str], index: int, inline: str = "") -> str:
    """The gloss under a feed item, if the report wrote one.

    Feed-shaped routine reports put the link on one line and a sentence of
    context either after it on the same line ("— summary") or on the next,
    indented. Both shapes have been written by the same routine in the same
    week, and a manifest that only read one of them offered a headline and
    nothing else for the other. So both count.

    The trailing classification (`**Why now:** ... **Provenance:** ...`) is
    bookkeeping for whoever tunes the routine, not for the reader, so it is cut.
    """
    parts: list[str] = []
    lead = _INLINE_NOTE_LEAD.sub("", inline).strip()
    if lead:
        parts.append(lead)
    for line in lines[index + 1 : index + 4]:
        if not line.strip() or not line.startswith((" ", "\t")):
            break
        parts.append(line.strip())
    note = " ".join(parts)
    note = _strip_meta_tail(note)
    note = _BOLD_RUN.sub(r"\1", note)
    note = strip_inline_markup(note)
    note = re.sub(r"\s+", " ", note).strip()
    return _NOTE_TRAIL.sub("", _NOTE_LEAD.sub("", note))

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
        list_match = _LIST_LINK.match(line)
        match = list_match or _TABLE_LINK.match(line)
        inline = ""
        if match:
            title, url = match.group(1).strip(), match.group(2)
            if list_match:
                # Whatever follows the link on the same line is the summary
                # when the writer chose the one-line shape.
                inline = line[match.end() :]
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
        note = _item_note(lines, position, inline)
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
    carried: bool = False

def collect_context_sources(
    ov: Path, routines: list[Routine], end: date,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Latest declared background references; inspect filenames/stat, never bodies."""
    contexts: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    root = ov.resolve()
    for routine in routines:
        if not routine.context:
            continue
        key = routine.context
        relative_dir = Path(routine.output_dir)
        pattern = Path(routine.file_pattern)
        if relative_dir.is_absolute() or ".." in relative_dir.parts or pattern.is_absolute() or ".." in pattern.parts:
            warnings.append(f"context {key}: unsafe output directory or file pattern")
            continue
        directory = ov / relative_dir
        try:
            resolved_dir = directory.resolve(strict=True)
            resolved_dir.relative_to(root)
            if not resolved_dir.is_dir():
                raise NotADirectoryError
        except (OSError, RuntimeError, ValueError):
            warnings.append(f"context {key}: output directory missing, unreadable, or outside vault")
            continue
        latest: tuple[date, str, str] | None = None
        try:
            for path in directory.glob(routine.file_pattern):
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(resolved_dir)
                    resolved.relative_to(root)
                    if not resolved.is_file():
                        continue
                    stat = resolved.stat()
                    when = date_in_text(path.name)
                    date_source = "filename" if when is not None else "mtime"
                    when = when if when is not None else date.fromtimestamp(stat.st_mtime)
                    if when > end:
                        continue
                    relative = _vault_relative(ov, path)
                    rank = (when, path.name, relative)
                    if latest is None or rank > latest:
                        latest = rank
                        contexts[key] = {
                            "routine": routine.name, "label": routine.label,
                            "path": relative, "name": path.name,
                            "date": when.isoformat(), "date_source": date_source,
                            "bytes": stat.st_size, "anchor": source_anchor(relative),
                        }
                except (OSError, RuntimeError, ValueError, OverflowError):
                    warning = f"context {key}: unsafe or unreadable matching source skipped"
                    if warning not in warnings:
                        warnings.append(warning)
        except (OSError, ValueError):
            warnings.append(f"context {key}: source discovery failed")
        if key not in contexts:
            warnings.append(f"context {key}: no eligible source at or before {end.isoformat()}")
    return contexts, warnings


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
    context_sources, context_warnings = collect_context_sources(ov, routines, end)

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

    carry: dict[str, Any] | None = None
    delivered_warnings: list[str] = []
    if mode == "daily" and not unacked and days is None and since is None:
        # Files dated just before the window that no earlier day's digest
        # delivered: a routine that finished after yesterday's morning run.
        delivered, delivered_warnings = load_delivered_state(ov)
        carry_start = start - timedelta(days=DAILY_CARRY_DAYS)
        carried = 0
        already = 0
        for routine in active:
            directory = ov / routine.output_dir
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob(routine.file_pattern), key=lambda p: p.name):
                when, when_source = file_date(path)
                if not (carry_start <= when < start):
                    continue
                delivered_on = delivered.get(_vault_relative(ov, path))
                if delivered_on and delivered_on < end.isoformat():
                    already += 1
                    continue
                if len(sources) >= max_files:
                    truncated = True
                    break
                source = _build_source(ov, path, routine, when, when_source, excerpt_chars, max_items)
                source.carried = True
                sources.append(source)
                carried += 1
            if truncated:
                break
        carry = {"days": DAILY_CARRY_DAYS, "files": carried, "already_delivered": already}

    sources.sort(key=lambda s: (s.date, s.label, s.name), reverse=True)
    lanes = _group_lanes(sources)
    updates, update_warnings = collect_digest_updates(
        ov,
        mode=mode,
        start=start,
        end=end,
    )
    update_warnings = delivered_warnings + update_warnings
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
        "context_sources": context_sources,
        "context_warnings": context_warnings,
        "acks": ack_targets,
        **({"carry": carry} if carry is not None else {}),
    }

def collect_health(
    ov: Path,
    routines: list[Routine],
    acks: dict[str, str],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Fleet output, Prefect run state, and review debt for the local-date window."""
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
    state_unavailable = False
    since = datetime.combine(start, datetime.min.time()).astimezone()
    names = {routine.name for routine in included if routine.execution == "local"}
    runs = []
    if names:
        import routine_status

        try:
            runs = routine_status.recent_runs(since, model_only=True)
        except routine_status.StatusUnavailable:
            state_unavailable = True
    for run in runs:
        if run.get("routine") not in names:
            continue
        started = run.get("expected_start_time") or run.get("start_time")
        if not isinstance(started, datetime) or not (start <= started.astimezone().date() <= end):
            continue
        state = run.get("state")
        if state == "COMPLETED":
            completed += 1
        elif state in {"FAILED", "CRASHED", "CANCELLED"} and run.get("state_name") != "Deferred":
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
        "state_unavailable": state_unavailable,
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
    except OSError as exc:
        raise SystemExit(f"routine output unreadable: {_vault_relative(ov, path)}: {exc}") from exc
    meta, body = parse_frontmatter(text)
    units = split_units(text)
    urls = []
    seen: set[str] = set()
    for match in _SOURCE_URL.finditer(text):
        url = match.group(1).strip().strip("\"'")
        if url.startswith("http") and url not in seen:
            seen.add(url)
            urls.append(url)
    keep = {"date", "type", "slug", "signal_type", "source_type", "source_tier", "status", "item_count", "window", "channels_reached"}
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
    if source.carried:
        data["carried"] = True
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
