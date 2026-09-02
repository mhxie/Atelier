#!/usr/bin/env python3
"""routine_digest.py: Roll up scheduled-routine outputs into one morning document.

Why this exists: scheduled routines write dated reports into a dozen
directories under $OV. `cues.py check_routine_outputs` only says "N new outputs,
go read them"; nothing turned that pile into something readable away from a
terminal, and review debt had reached hundreds of unread files.

This script is the deterministic half of the digest. It selects which routine
outputs fall in a window, extracts a bounded, quotable projection of each one,
renders one semantic HTML document, and writes it into $OV as the run's
canonical artifact. The judgment half -- the cross-source overview prose -- is
written by the `/digest` command and injected through `--overview` as
structured JSON, so the renderer stays deterministic and testable.

An optional private `$OV/_meta/digest_updates.toml` also declares append-only
Markdown ledgers whose rows are mandatory status updates. New rows are rendered
deterministically above the overview: daily delivery uses a write cursor so a
late-day change reaches the next morning exactly once, while weekly delivery
uses the report's seven-day window. The cursor records delivery only and is
separate from `routine_acks.json`, which means the user reviewed the material.

Per `protocols/remote-routines.md`, the artifact under $OV is the source of
truth and the morning email is a presentation of it. Delivery is deterministic
and lives in `mail`, not in the model: the recipient comes from private config,
the body is the artifact verbatim, and nothing between the two can redirect a
message. That is not only a safety preference. The Codex Gmail plugin marks
`send_email` as requiring approval, and unattended routines run under
`approval_policy = "never"`, so a model-sent message is not possible there at
all (verified 2026-08-31).

Nothing generated here is ever pushed into Readwise: Reader stores originals,
and the digest links into it rather than adding to it.

The document's first screen is the action surface from `daily_brief.py --json`,
passed in through `--brief`. It renders above the overview because it is the
only part read before the day's deep work; anything placed above it spends that
attention. Without `--brief` the document is intel only, which is the right
shape for the weekly roll-up.

Stdlib-only on purpose. The digest is summary-depth, so no source body is
converted wholesale; only a bounded inline-markdown subset (links, bold, code)
appears in overview bullets. That removes any need for a markdown dependency.

Subcommands:
    collect     select routine outputs in a window -> manifest JSON
    render      manifest (+ optional brief and overview) -> HTML on stdout
    write       same render, into the routine's declared $OV output directory
    mail        send an artifact to the account in $OV/_meta/mail.toml
    ack         advance $OV/_meta/routine_acks.json past the digested files

Typical flow (the `/digest` command automates the middle step; `$PY` is the
interpreter from `scripts/find_python.sh`, never `uv run`, because the routine
sandbox cannot sync uv and these scripts are stdlib-only):

    $PY scripts/routine_digest.py collect --mode daily --json > m.json
    $PY scripts/daily_brief.py --json --out brief.json      # daily only
    # ... model reads m.json and brief.json, writes overview.json ...
    $PY scripts/routine_digest.py write --manifest m.json \
        --brief brief.json --overview overview.json
    $PY scripts/routine_digest.py mail --html <artifact> --subject "<title>"
    $PY scripts/routine_digest.py ack --manifest m.json

`write` finds the digest's own registry row by its `include = false` mark;
`--routine <name>` is only needed when several rows are excluded.

Registry: `$OV/_meta/routine_watch.toml` is the source of truth for which
routines exist, where they write, and which filenames belong to them. Per-routine
digest policy is optional and lives in the same rows, so this script stays
vault-agnostic:

    [[routine]]
    name = "..."
    output_dir = "..."
    file_pattern = "*.md"
    digest = { lane = "Finance", include = true }

`include = false` drops a routine from every digest. The only routine excluded
by default is the public harness maintenance job `autoevo-nightly`, whose output
is applied-decay bookkeeping rather than gathered intel; `--include-maintenance`
overrides that.

Window semantics: `--mode daily` covers one effective day, `--mode weekly` the
7 days ending on it. Before 03:00 local the effective day is the previous date,
matching the day boundary the rest of the harness uses. `--unacked` ignores the
window entirely and takes everything past each directory's ack, for clearing
backlog.

Exit codes: 0 on success. An empty window is a success that writes nothing, by
design: a digest with no content is worse than an absent one, and the routine
reports the empty window instead of mailing a hollow document. 1 on a real
failure.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import PathsError, atomic_write, fmt, vault_root  # noqa: E402

MANIFEST_SCHEMA = 1
OVERVIEW_SCHEMA = 1
BRIEF_SCHEMA = 1  # must match daily_brief.BRIEF_SCHEMA
CONTEXT_SCHEMA = 1  # must match daily_context.CONTEXT_SCHEMA

# Optional private append-only ledgers whose new rows must appear in digests.
# Paths and labels stay under $OV so the public harness never names a private
# tracker. Daily delivery uses its own cursor: routine_acks.json means
# "reviewed", while this state means only "written into a digest artifact".
DIGEST_UPDATES_CONFIG = "_meta/digest_updates.toml"
DIGEST_UPDATES_STATE = "_meta/digest_update_state.json"

# Public harness routine whose output is maintenance bookkeeping, not intel.
# Named here (not in the private vault registry) because the name is already
# public in scripts/launchd/ and .claude/commands/.
MAINTENANCE_ROUTINES = {"autoevo-nightly"}

DEFAULT_EXCERPT_CHARS = 800
DEFAULT_MAX_ITEMS = 15
DEFAULT_MAX_FILES = 200

# Gmail clips a message past roughly 102 KB and hides the rest behind a "View
# entire message" link. That is a warning, not a failure: the document is
# ordered so the clipped tail is the source index, which is navigation rather
# than content. Worth reporting so a runaway window is visible.
GMAIL_CLIP_BYTES = 102_000

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

# Research first. The fleet writes fourteen finance files for every research
# one, and the curated depth is picked from what the manifest shows first, so
# lane order is the cheapest lever on which lane the reader's minutes go to.
# `deep_read_lane_gap` is the second lever: it names the miss when the pick
# still skips research on a window that had it.
LANE_ORDER = ["Research", "Tech feed", "Finance", "Toolcraft", "Career", "Findings"]
RESEARCH_LANE = "Research"

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

_INLINE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_ANY_MD_LINK = re.compile(r"\[([^\]]+)\]\(\s*<?[^)]*>?\s*\)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_INLINE_BOLD = re.compile(r"\*\*([^*]+)\*\*")

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

# ---------------------------------------------------------------- style
#
# Every rule is inline. Mail clients strip <style> blocks with no warning and no
# fallback, so a stylesheet would look right in a browser and arrive unstyled in
# the one place this document is actually read. Fonts are system stacks for the
# same reason: a webfont link is dropped, and the CJK fallbacks matter because
# most of this document's prose is Chinese.
#
# The weights below are the document's argument, not decoration. The first
# screen gets the only card and the only accent because it is the only part read
# before the day's deep work; the source index is muted because it is navigation
# that already costs about a thousand words.

_FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', "
    "'Hiragino Sans GB', 'Noto Sans SC', Roboto, Helvetica, Arial, sans-serif"
)
_MONO = "Menlo, Consolas, 'SF Mono', ui-monospace, monospace"
_SERIF = "Georgia, 'Songti SC', 'Noto Serif SC', serif"
# Neutrals carry a slight green bias so they read as chosen rather than
# inherited; the accent is one workshop green and it never doubles as a status
# colour. Status has its own three: urgent red, amber, ok green.
_INK = "#17201c"
_MUTED = "#5c6862"
_FAINT = "#8b968f"
_RULE = "#d9dfd9"
_ACCENT = "#0f6b5c"
_LINK = "#0f6b5c"
_CARD = "#ffffff"
_CHIP = "#eef1ec"
_URGENT = "#b23d27"
_AMBER = "#b8860b"
_OK = "#2f7d4f"
_BAR_TRACK = "#e3e8e3"

_S_WRAP = (
    f"max-width:640px;margin:0 auto;padding:24px 20px 32px;font-family:{_FONT};"
    f"font-size:15px;line-height:1.65;color:{_INK};"
)
_S_MAST = f"width:100%;border-collapse:collapse;border-bottom:2px solid {_INK};"
_S_H1 = (
    f"margin:0;font-family:{_SERIF};font-size:26px;font-weight:600;line-height:1.15;"
    "letter-spacing:-0.01em;padding:0 0 10px;"
)
_S_H1_DATE = f"color:{_ACCENT};"
_S_MAST_SIDE = "padding:0 0 10px 12px;text-align:right;vertical-align:bottom;"
_S_WEATHER = f"display:block;font-size:13px;color:{_INK};white-space:nowrap;"
_S_WEATHER_SUB = (
    f"display:block;font-family:{_MONO};font-size:11px;color:{_FAINT};white-space:nowrap;"
)
_S_META = f"margin:0 0 22px;font-size:12px;color:{_FAINT};"
_S_CARD = "margin:0 0 6px;padding:24px 0 0;"
_S_CARD_H = (
    f"margin:0 0 8px;font-size:12px;font-weight:600;letter-spacing:0.08em;"
    f"text-transform:uppercase;color:{_ACCENT};"
)
_S_CARD_H_N = (
    f"font-family:{_MONO};font-weight:500;color:{_FAINT};letter-spacing:0;text-transform:none;"
)
_S_GROUP_HOT = f"margin:12px 0 4px;font-size:14px;font-weight:650;color:{_INK};"
_S_GROUP_COOL = f"margin:12px 0 4px;font-size:13px;font-weight:600;color:{_MUTED};"
_S_LEDGER = f"width:100%;border-collapse:collapse;border-top:1px solid {_RULE};"
_S_LEDGER_DUE = (
    f"width:56px;padding:10px 0;border-bottom:1px solid {_RULE};vertical-align:top;"
    f"white-space:nowrap;font-family:{_MONO};font-size:12px;font-weight:500;"
)
_S_LEDGER_ITEM = (
    f"padding:10px 0 10px 12px;border-bottom:1px solid {_RULE};vertical-align:top;"
    "line-height:1.55;"
)
_S_LEDGER_HEAD = (
    f"padding:12px 0 2px;font-size:12px;font-weight:600;color:{_MUTED};"
)
_S_UL = "margin:4px 0 0;padding-left:19px;"
_S_ITEM = "margin:0 0 6px;line-height:1.5;"
_S_TRACE = (
    f"font-family:{_MONO};font-size:11px;color:{_FAINT};background:{_CHIP};"
    "padding:1px 6px;border-radius:3px;white-space:nowrap;"
)
_S_LI = "margin:0 0 5px;"
_S_H2 = (
    f"margin:26px 0 8px;font-family:{_SERIF};font-size:19px;font-weight:600;"
    "letter-spacing:-0.005em;"
)
_S_H2_N = f"font-family:{_MONO};font-size:12px;font-weight:400;color:{_FAINT};margin-left:8px;"
_S_LEAD = (
    f"margin:28px 0 6px;padding-left:14px;border-left:3px solid {_ACCENT};"
    f"font-family:{_SERIF};font-size:18px;line-height:1.55;font-weight:500;"
)
_S_P = "margin:0 0 8px;"
_S_SMALL = f"font-size:12px;color:{_MUTED};"
_S_CODE = (
    f"font-family:{_MONO};font-size:11.5px;background:{_CHIP};color:{_MUTED};"
    "padding:1px 5px;border-radius:4px;"
)
_S_LINK = f"color:{_LINK};text-decoration:none;border-bottom:1px solid {_LINK};"
_S_SRC_LINK = (
    f"font-family:{_MONO};font-size:11px;font-weight:400;color:{_ACCENT};"
    "text-decoration:none;white-space:nowrap;"
)
_S_INDEX_H = f"margin:18px 0 6px;font-size:13px;font-weight:650;color:{_MUTED};"
_S_INDEX_UL = "margin:0;padding-left:18px;list-style:none;"
_S_INDEX_LI = f"margin:0 0 11px;font-size:13px;line-height:1.5;color:{_MUTED};"
_S_NOTE = f"margin:18px 0 0;font-size:12px;color:{_FAINT};"
_S_STAT_TABLE = (
    "width:100%;border-collapse:collapse;margin:0;"
    f"border-bottom:1px solid {_RULE};"
)
_S_STAT_CELL = f"padding:14px 6px;vertical-align:top;border-left:1px solid {_RULE};"
_S_STAT_CELL_FIRST = "padding:14px 6px 14px 0;vertical-align:top;"
_S_STAT_NUM = (
    f"display:block;font-family:{_MONO};font-size:19px;font-weight:500;line-height:1.1;"
    "white-space:nowrap;"
)
_S_STAT_LABEL = (
    f"display:block;font-size:10.5px;color:{_FAINT};letter-spacing:0.03em;margin-top:4px;"
    "white-space:nowrap;"
)
_S_QUOTA_TABLE = f"width:100%;border-collapse:collapse;border-bottom:1px solid {_RULE};"
_S_QUOTA_CELL = "padding:14px 14px 14px 0;vertical-align:top;width:50%;"
_S_QUOTA_CELL_NEXT = f"padding:14px 0 14px 14px;vertical-align:top;width:50%;border-left:1px solid {_RULE};"
_S_QUOTA_NAME = "font-weight:600;"
_S_QUOTA_SUB = f"font-family:{_MONO};font-size:11px;color:{_FAINT};"
_S_QUOTA_BAR = "width:100%;border-collapse:collapse;margin:6px 0 4px;"
_S_QUOTA_SEG = "height:5px;font-size:0;line-height:0;"
_S_QUOTA_LEFT = f"font-family:{_MONO};font-size:12px;font-weight:600;"
_S_QUOTA_META = f"font-family:{_MONO};font-size:11px;color:{_MUTED};"
_S_QUOTA_SNAP = f"font-family:{_MONO};font-size:10.5px;color:{_FAINT};"

_S_FOLD = "width:100%;border-collapse:collapse;margin:34px 0 18px;"
_S_FOLD_RULE = f"border-top:1px dashed {_RULE};font-size:0;line-height:0;"
_S_FOLD_TEXT = (
    f"padding:0 14px;font-family:{_MONO};font-size:12px;color:{_FAINT};white-space:nowrap;"
)
_S_COST = f"font-size:11px;color:{_FAINT};font-weight:400;margin-left:6px;"
_S_DEEP_ITEM = f"margin:0 0 16px;padding-left:11px;border-left:2px solid {_RULE};"
_S_DEEP_HEAD = "margin:0 0 3px;font-size:13.5px;font-weight:650;"
_S_DEEP_BODY = f"margin:0;font-size:13.5px;line-height:1.62;color:{_INK};"
_S_RETRO_META = f"font-size:11px;color:{_FAINT};margin-left:6px;font-weight:400;"

_S_MD_P = "margin:0 0 9px;font-size:13.5px;line-height:1.66;"
_S_MD_H2 = "margin:14px 0 6px;font-size:14px;font-weight:650;"
_S_MD_H3 = f"margin:12px 0 5px;font-size:13px;font-weight:650;color:{_MUTED};"
_S_MD_UL = "margin:0 0 9px;padding-left:19px;"
_S_MD_LI = "margin:0 0 4px;font-size:13.5px;line-height:1.6;"
_S_MD_TABLE = (
    "width:100%;border-collapse:collapse;margin:0 0 10px;font-size:12.5px;"
)
_S_MD_TH = (
    f"text-align:left;padding:5px 7px;border-bottom:1px solid {_RULE};"
    f"font-weight:650;color:{_MUTED};"
)
_S_MD_TD = f"padding:5px 7px;border-bottom:1px solid {_RULE};vertical-align:top;"

_S_ART = f"margin:0;padding:14px 0;border-top:1px solid {_RULE};"
_S_ART_FIRST = "margin:0;padding:0 0 14px;"
_S_ART_TITLE = (
    f"margin:0;font-family:{_SERIF};font-size:16.5px;font-weight:600;line-height:1.35;"
)
_S_ART_META = (
    f"display:block;margin:3px 0 6px;font-family:{_MONO};font-size:11px;color:{_FAINT};"
    "font-weight:400;"
)
_S_ART_WHY = f"margin:0 0 4px;color:{_ACCENT};font-weight:500;"
_S_ART_ABSTRACT = f"margin:0;color:{_MUTED};"

_S_LAB_TABLE = "width:100%;border-collapse:collapse;"
_S_LAB_NAME = f"width:112px;padding:9px 10px 9px 0;vertical-align:top;border-top:1px solid {_RULE};"
_S_LAB_CAT = f"font-family:{_MONO};font-size:10.5px;color:{_FAINT};letter-spacing:0.04em;"
_S_LAB_TEXT = f"padding:9px 0;vertical-align:top;border-top:1px solid {_RULE};"
_S_LAB_NOTE = f"padding:9px 0 0;border-top:1px solid {_RULE};font-size:13px;color:{_MUTED};"

_S_DR_GROUP = (
    f"padding:22px 0 6px;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;"
    f"color:{_ACCENT};font-weight:600;"
)
_S_DR_ITEM = f"padding:14px 0;border-top:1px solid {_RULE};"
_S_DR_TITLE = (
    f"margin:0 0 6px;font-family:{_SERIF};font-size:16px;font-weight:600;line-height:1.35;"
)
_S_DR_UL = f"margin:0 0 8px;padding-left:18px;color:{_INK};"
_S_DR_LI = "margin:0 0 5px;"
_S_DR_WHY = f"margin:0;padding-left:12px;border-left:2px solid {_ACCENT};color:{_MUTED};"

_S_RULE = f"border:0;border-top:1px solid {_RULE};margin:26px 0 12px;"
_S_COLOPHON = f"margin:18px 0 0;font-family:{_MONO};font-size:11px;color:{_FAINT};line-height:1.6;"


UNIT_EXCERPT_CHARS = 320
MAX_UNITS_PER_FILE = 8

# Source-index render budget. The index is navigation, not content.
_INDEX_ITEM_CAP = 5
_INDEX_EXCERPT_CAP = 180


# ---------------------------------------------------------------- registry


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
    max_lines: int = DEFAULT_ROUTINE_LINES
    # True only when the row itself says `include = false`; the default
    # exclusion of maintenance routines does not count. `write` uses this to
    # find the digest's own row when no --routine is given.
    excluded_explicitly: bool = False


def load_routines(ov: Path) -> list[Routine]:
    """Parse $OV/_meta/routine_watch.toml into Routine rows.

    Rows without an output_dir are skipped: they cannot contribute files.
    """
    config_path = ov / "_meta" / "routine_watch.toml"
    if not config_path.is_file():
        raise SystemExit(f"routine registry missing: {fmt(config_path)}")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SystemExit(f"routine registry unreadable: {exc!r}") from exc

    routines: list[Routine] = []
    for row in config.get("routine", []):
        output_dir = row.get("output_dir")
        if not output_dir:
            continue
        name = str(row.get("name") or output_dir)
        digest_cfg = row.get("digest") or {}
        if not isinstance(digest_cfg, dict):
            digest_cfg = {}
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
                max_lines=max_lines,
                excluded_explicitly=digest_cfg.get("include") is False,
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


# ------------------------------------------------------- append-only updates


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


# ---------------------------------------------------------------- window


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


# ---------------------------------------------------------------- extraction


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


def humanize_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip()


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


# ---------------------------------------------------------------- collect


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


def _vault_relative(ov: Path, path: Path) -> str:
    try:
        return str(path.relative_to(ov))
    except ValueError:
        return str(path)


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


def source_anchor(path: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return f"src-{slug}"


def iter_sources(manifest: dict[str, Any]):
    for lane in manifest.get("lanes", []):
        for source in lane.get("sources", []):
            yield lane.get("lane", ""), source


# ---------------------------------------------------------------- render


def inline_html(text: str) -> str:
    """Escape, then apply the bounded inline-markdown subset.

    Escaping first is what makes this safe: the escaped text still contains
    literal `[`, `]`, `(`, `*`, and backticks, so the patterns below match,
    while any HTML the model wrote is inert.
    """
    out = html_mod.escape(str(text))
    out = _INLINE_LINK.sub(
        lambda m: f'<a href="{m.group(2)}" style="{_S_LINK}">{m.group(1)}</a>', out
    )
    out = _INLINE_CODE.sub(f'<code style="{_S_CODE}">\\1</code>', out)
    out = _INLINE_BOLD.sub(r"<strong>\1</strong>", out)
    return out


# Reading speeds used to price a section. Deliberately conservative: the number
# exists so the reader can decline a section without opening it, and an estimate
# that reads low teaches them to distrust it.
_CJK_PER_MINUTE = 330
_WORDS_PER_MINUTE = 220
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
_TAGS = re.compile(r"<[^>]+>")


def reading_minutes(html: str) -> int:
    """Rough minutes to read rendered HTML, never less than 1 for non-empty text.

    This is what makes a long document safe to send. The reader agreed to a
    five-minute scan; everything past the fold has to carry its own price tag or
    the length becomes a demand rather than an offer.
    """
    text = _TAGS.sub(" ", html)
    cjk = len(_CJK.findall(text))
    latin_words = len([w for w in re.sub(_CJK.pattern, " ", text).split() if w.strip()])
    minutes = cjk / _CJK_PER_MINUTE + latin_words / _WORDS_PER_MINUTE
    if not cjk and not latin_words:
        return 0
    return max(1, round(minutes))


def digest_title(manifest: dict[str, Any]) -> str:
    window = manifest.get("window", {})
    since, until = window.get("since", "?"), window.get("until", "?")
    if manifest.get("selection") == "unacked":
        return f"Atelier Digest — backlog through {until}"
    if manifest.get("mode") == "daily" or since == until:
        return f"Atelier Daily — {until}"
    return f"Atelier Weekly — {since} → {until}"


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


def render(
    manifest: dict[str, Any],
    overview: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    retrospect: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """One document, two layers, with the boundary drawn on the page.

    The reader agreed to five minutes and may choose to spend more depending on
    mood and on what the scan layer showed them. That only works if the scan
    layer is self-sufficient and if the fold is visible: a reader who cannot see
    where the short version ends cannot safely stop reading.

    So everything above the fold is complete on its own, everything below is
    optional depth, and each half carries an honest minute count. Sections do
    not link to their deep counterparts because a mail client rewrites
    in-document anchors; they are found by name instead.
    """
    overview = overview or {}
    context = context or {}
    counts = manifest.get("counts", {})
    parts: list[str] = []
    # lang lets mail clients pick CJK fallbacks and hyphenation rules for the
    # mixed Chinese and English body; overflow-wrap keeps a long URL from
    # widening the column on a phone.
    parts.append(f'<div lang="zh-CN" style="{_S_WRAP}overflow-wrap:anywhere;">')
    parts.append(_render_masthead(manifest, context))

    # Provenance counts are bookkeeping, not news: they move to the colophon
    # so the masthead's second slot can hold the one external fact that changes
    # the day's plan, the weather where the day is spent.
    generated = str(manifest.get("generated", ""))
    meta_bits = [f"{counts.get('bytes', 0) // 1024} KB 源文本"]
    if counts.get("updates"):
        meta_bits.append(f"{counts['updates']} 条状态更新")
    if generated:
        meta_bits.append(f"生成于 {html_mod.escape(generated[11:16] or generated)}")
    if context.get("weather"):
        meta_bits.append("天气 Open-Meteo，地点取自当天日程")

    if manifest.get("health"):
        parts.append(_render_health(manifest["health"], brief))
    if context.get("quota"):
        parts.append(_render_quota(context))
    if context.get("warnings"):
        rendered = "<br>".join(
            f"! {html_mod.escape(str(warning))}" for warning in context["warnings"]
        )
        parts.append(f'<p style="{_S_SMALL}margin:8px 0 0;">{rendered}</p>')

    # The action surface goes above everything else, including the overview:
    # it is the only part of this document that is read before the day's deep
    # work, and anything placed above it costs that attention.
    if brief:
        parts.append(_render_brief(brief))

    # Configured ledger rows are a delivery contract, not an editorial choice.
    # Render them before model-written overview prose so every new row reaches
    # both daily and weekly artifacts even when no cross-source synthesis cites it.
    if manifest.get("updates"):
        parts.append(_render_updates(manifest["updates"]))
    if manifest.get("update_warnings"):
        rendered = "<br>".join(
            f"! {html_mod.escape(str(warning))}"
            for warning in manifest["update_warnings"]
        )
        parts.append(f'<p style="{_S_SMALL}">{rendered}</p>')

    if overview.get("headline"):
        parts.append(f'<p style="{_S_LEAD}">{inline_html(overview["headline"])}</p>')

    sections = overview.get("sections") or []
    if sections:
        for section in sections:
            parts.append(_render_section(section, manifest))
    else:
        parts.append(f'<p style="{_S_SMALL}">No overview supplied; source index only.</p>')

    frontier = _render_frontier(
        overview.get("frontier_labs"), str(manifest.get("window", {}).get("until", ""))
    )
    if frontier:
        parts.append(frontier)

    feed_health = _render_feed_health(manifest.get("feeds") or {})

    briefs = _render_routine_briefs(overview.get("routines") or [], manifest)
    if briefs or feed_health:
        parts.append(f'<h2 style="{_S_H2}">routine 摘要</h2>')
        if feed_health:
            parts.append(feed_health)
        parts.append(briefs)

    articles = _render_articles(overview.get("articles") or [])
    if articles:
        parts.append(f'<h2 style="{_S_H2}">新文章{_articles_badge(overview.get("articles") or [])}</h2>')
        parts.append(articles)

    # ---- below the fold ----
    depth: list[str] = []

    # Curated depth wins over raw bodies. The model picks the few findings
    # worth the reader's minutes and rewrites them in the reader's language;
    # the raw dump stays as the fallback for windows nobody curated.
    curated = _render_deep_read_curated(overview.get("deep_read"))
    feed_items = _render_feed_items(manifest) if curated else ""
    if curated:
        deep_read = curated + feed_items
        badge = _deep_read_badge(overview.get("deep_read"), manifest)
    else:
        try:
            deep_read = _render_deep_read(manifest, vault_root())
        except PathsError:
            deep_read = _render_deep_read(manifest)
        badge = ""
    if deep_read:
        depth.append(f'<h2 style="{_S_H2}">情报详读{badge}{{cost_deep}}</h2>')
        gap = deep_read_lane_gap(overview.get("deep_read"), manifest)
        if gap:
            depth.append(
                f'<p style="{_S_SMALL}margin:0 0 8px;color:{_URGENT};">! {html_mod.escape(gap)}</p>'
            )
        depth.append(deep_read)

    retro = _render_retrospect(retrospect or [])
    if retro:
        depth.append(f'<h2 style="{_S_H2}">随机回顾{{cost_retro}}</h2>')
        depth.append(retro)

    depth.append(f'<h2 style="{_S_H2}">来源索引 / Source index</h2>')
    if not any(True for _ in iter_sources(manifest)):
        depth.append(f'<p style="{_S_SMALL}">No routine output in this window.</p>')
    for lane in manifest.get("lanes", []):
        depth.append(
            f'<h3 style="{_S_INDEX_H}">{html_mod.escape(str(lane.get("lane", "")))} '
            f'({lane.get("files", 0)})</h3>'
        )
        depth.append(f'<ul style="{_S_INDEX_UL}">')
        for source in lane.get("sources", []):
            depth.append(_render_source(source))
        depth.append("</ul>")

    if manifest.get("truncated"):
        depth.append(
            f'<p style="{_S_SMALL}">Selection was truncated by --max-files; '
            "narrow the window.</p>"
        )
    if manifest.get("skipped_routines"):
        skipped = ", ".join(html_mod.escape(s) for s in manifest["skipped_routines"])
        depth.append(f'<p style="{_S_NOTE}">Excluded from this digest: {skipped}.</p>')

    depth.append(f'<hr style="{_S_RULE}">')
    depth.append(
        f'<p style="{_S_COLOPHON}">{" · ".join(meta_bits)}<br>'
        f'Generated by <code style="{_S_CODE}">scripts/routine_digest.py</code> from '
        f'<code style="{_S_CODE}">$OV/_meta/routine_watch.toml</code> and optional '
        f'<code style="{_S_CODE}">$OV/_meta/digest_updates.toml</code>. '
        "Paths are relative to the vault root.</p>"
    )
    # Price each half only after both are built, so the numbers describe what
    # was actually rendered rather than what was intended.
    depth_html = "\n".join(depth)
    depth_html = depth_html.replace(
        "{cost_deep}", _cost_badge(reading_minutes(deep_read))
    ).replace("{cost_retro}", _cost_badge(reading_minutes(retro)))
    scan_minutes = reading_minutes("\n".join(parts))
    depth_minutes = reading_minutes(depth_html)

    parts.append(
        f'<table role="presentation" style="{_S_FOLD}"><tr>'
        f'<td style="{_S_FOLD_RULE}">&nbsp;</td>'
        f'<td style="{_S_FOLD_TEXT}">以上 {scan_minutes} 分钟读完 · '
        f"以下 {depth_minutes} 分钟，按需</td>"
        f'<td style="{_S_FOLD_RULE}">&nbsp;</td></tr></table>'
    )
    parts.append(depth_html)
    parts.append("</div>")
    return "\n".join(parts) + "\n"


def _cost_badge(minutes: int) -> str:
    return f'<span style="{_S_COST}">{minutes} 分钟</span>' if minutes else ""


def _render_masthead(manifest: dict[str, Any], context: dict[str, Any]) -> str:
    """Title on the left, the day's weather on the right.

    The weather is the only masthead fact that is not about the document
    itself, and it earns the slot because it changes the day's plan; the
    provenance counts that used to sit here went to the colophon.
    """
    title = digest_title(manifest)
    head, sep, tail = title.partition(" — ")
    if sep:
        title_html = (
            f'{html_mod.escape(head)} <span style="{_S_H1_DATE}">{html_mod.escape(tail)}</span>'
        )
    else:
        title_html = html_mod.escape(title)
    side = ""
    weather = context.get("weather") if isinstance(context, dict) else None
    if isinstance(weather, dict) and weather.get("place"):
        line = (
            f'<b>{html_mod.escape(str(weather["place"]))}</b> '
            f'{html_mod.escape(str(weather.get("tmin", "?")))}–'
            f'{html_mod.escape(str(weather.get("tmax", "?")))}°C'
            f' · {html_mod.escape(str(weather.get("summary", "")))}'
        )
        if weather.get("precip_probability") is not None:
            line += f' · 降水 {html_mod.escape(str(weather["precip_probability"]))}%'
        hours = " · ".join(
            f'{int(h["hour"])}:00 {html_mod.escape(str(h["temp"]))}°'
            for h in weather.get("hours") or []
            if isinstance(h, dict) and "hour" in h and "temp" in h
        )
        sub_bits = [b for b in (hours, str(weather.get("date") or "")) if b]
        side = f'<span style="{_S_WEATHER}">{line}</span>'
        if sub_bits:
            side += f'<span style="{_S_WEATHER_SUB}">{" · ".join(sub_bits)}</span>'
    return (
        f'<table role="presentation" style="{_S_MAST}"><tr>'
        f'<td style="vertical-align:bottom;"><h1 style="{_S_H1}">{title_html}</h1></td>'
        f'<td style="{_S_MAST_SIDE}">{side}</td></tr></table>'
    )


_HEADING_OVERDUE = re.compile(r"(\d+) 条逾期")
_FIRST_INT = re.compile(r"(\d+)")


def _brief_counts(brief: dict[str, Any] | None) -> dict[str, int]:
    """Counts the stat strip borrows from the brief: recurring overdue and
    review debt. Parsed from the group headings the brief already prints, so
    the strip and the ledger can never disagree."""
    counts: dict[str, int] = {}
    for group in (brief or {}).get("groups") or []:
        kind = group.get("kind")
        heading = str(group.get("heading", ""))
        if kind == "recurring":
            match = _HEADING_OVERDUE.search(heading)
            counts["recurring_overdue"] = int(match.group(1)) if match else 0
        elif kind == "review":
            items = group.get("items") or []
            match = _FIRST_INT.search(heading)
            counts["review_debt"] = len(items) if items else (int(match.group(1)) if match else 0)
    return counts


def _render_health(health: dict[str, Any], brief: dict[str, Any] | None = None) -> str:
    """Six numbers, so a thin window cannot be mistaken for a quiet day.

    A digest that only summarises what routines said reads identically whether
    the fleet is healthy or half of it is failing. These are the counts that
    tell those apart, and the failure count turns red on its own rather than
    waiting for anyone to notice a low output number. The last two come from
    the brief when it exists: overdue recurring obligations and review debt,
    the two counts that used to hide in a sentence under the ledger.
    """
    failed = int(health.get("failed", 0))
    debt = int(health.get("review_debt", 0))
    cells = [
        (f'{health.get("reported", 0)} / {health.get("declared", 0)}', "有产出", _ACCENT),
        (str(health.get("completed", 0)), "完成", _INK),
        (str(failed), "失败", _URGENT if failed else _FAINT),
        (str(debt), "待 review", _URGENT if debt > 40 else _INK),
    ]
    extra = _brief_counts(brief)
    if "recurring_overdue" in extra:
        overdue = extra["recurring_overdue"]
        cells.append((str(overdue), "recurring 逾期", _URGENT if overdue else _FAINT))
    if "review_debt" in extra:
        cells.append((str(extra["review_debt"]), "review 债", _INK))
    out = [f'<table role="presentation" style="{_S_STAT_TABLE}"><tr>']
    for index, (value, label, colour) in enumerate(cells):
        cell_style = _S_STAT_CELL_FIRST if index == 0 else _S_STAT_CELL
        out.append(
            f'<td style="{cell_style}">'
            f'<span style="{_S_STAT_NUM}color:{colour};">{html_mod.escape(value)}</span>'
            f'<span style="{_S_STAT_LABEL}">{html_mod.escape(label)}</span></td>'
        )
    out.append("</tr></table>")
    return "".join(out)


_QUOTA_COLOUR = {"ok": _OK, "low": _AMBER, "critical": _URGENT}


def _render_quota(context: dict[str, Any]) -> str:
    """Each harness's weekly window as a bar of what is left.

    The bar fills with the remaining share, not the used one, because the
    number the reader acts on is how much is still there. Its colour comes
    from daily_context's level, and the snapshot age stays visible: a passive
    snapshot can be a day old, and hiding that would turn a stale number into
    a confident one.
    """
    entries = [e for e in context.get("quota") or [] if isinstance(e, dict) and e.get("name")]
    if not entries:
        return ""
    cells = []
    for index, entry in enumerate(entries):
        left = max(0, min(100, int(entry.get("left_percent", 0))))
        colour = _QUOTA_COLOUR.get(str(entry.get("level", "")), _INK)
        bar = (
            f'<table role="presentation" style="{_S_QUOTA_BAR}"><tr>'
            f'<td width="{left}%" style="{_S_QUOTA_SEG}background:{colour};">&nbsp;</td>'
            f'<td style="{_S_QUOTA_SEG}background:{_BAR_TRACK};">&nbsp;</td></tr></table>'
        )
        age = entry.get("snapshot_age_hours")
        snap = (
            f'<span style="{_S_QUOTA_SNAP}"> · 快照 {html_mod.escape(str(age))}h 前</span>'
            if age is not None
            else ""
        )
        cells.append(
            f'<td style="{_S_QUOTA_CELL if index == 0 else _S_QUOTA_CELL_NEXT}">'
            f'<span style="{_S_QUOTA_NAME}">{html_mod.escape(str(entry["name"]))}</span> '
            f'<span style="{_S_QUOTA_SUB}">{html_mod.escape(str(entry.get("window", "")))}</span>'
            f"{bar}"
            f'<span style="{_S_QUOTA_LEFT}color:{colour};">剩 {left}%</span>'
            f'<span style="{_S_QUOTA_META}"> · {html_mod.escape(str(entry.get("reset_relative", "")))}</span>'
            f"{snap}</td>"
        )
    return (
        f'<p style="{_S_CARD_H}margin:14px 0 0;">Harness 周额度</p>'
        f'<table role="presentation" style="{_S_QUOTA_TABLE}"><tr>{"".join(cells)}</tr></table>'
    )


def _due_cell(item: dict[str, Any], tier: Any) -> str:
    """The countdown column: a number the eye can sort by."""
    days = item.get("days_left")
    if days is None:
        return f'<span style="color:{_FAINT};">·</span>'
    try:
        days = int(days)
    except (TypeError, ValueError):
        return f'<span style="color:{_FAINT};">·</span>'
    if days < 0:
        return f'<span style="color:{_URGENT};font-weight:600;">逾期 {-days}d</span>'
    if days == 0:
        return f'<span style="color:{_URGENT};font-weight:600;">今天</span>'
    if days == 1:
        return f'<span style="color:{_URGENT};font-weight:600;">明天</span>'
    colour = _ACCENT if tier == 1 else _MUTED
    return f'<span style="color:{colour};">{days}d</span>'


def _render_brief(brief: dict[str, Any]) -> str:
    """The first screen: group headings with their surviving bullets.

    Group headings are `<p><strong>` rather than `<h3>` so Reader's generated
    outline stays a map of the document's sections instead of filling up with
    one entry per count line. Warnings render inline, because a stale deadline
    index has to be visible in the pushed document too, not only in the terminal.
    """
    out = [f'<div style="{_S_CARD}">']
    groups = brief.get("groups") or []
    n_items = sum(len(g.get("items") or []) for g in groups)
    out.append(
        f'<p style="{_S_CARD_H}">今日 <span style="{_S_CARD_H_N}">'
        f'{html_mod.escape(str(brief.get("date", "")))} · {n_items} 件</span></p>'
    )
    if not groups:
        out.append(f'<p style="{_S_P}">今天没有关窗项、到期 TODO 或 review 债。</p>')
    else:
        # A ledger, not a list: the countdown sits in its own column so the
        # eye can sort by it, and a group heading is a row that spans both.
        out.append(f'<table role="presentation" style="{_S_LEDGER}">')
        for group in groups:
            # Tier 1 is forfeitable and inside its own lead time. It carries
            # the document's only heavy weight because it is the only thing
            # here whose cost is permanent; everything below slips rather
            # than disappears.
            heading_style = _S_GROUP_HOT if group.get("tier") == 1 else _S_GROUP_COOL
            out.append(
                f'<tr><td colspan="2" style="{_S_LEDGER_HEAD}">'
                f'<span style="{heading_style}margin:0;">'
                f'{html_mod.escape(str(group.get("heading", "")))}</span></td></tr>'
            )
            for item in group.get("items") or []:
                text = html_mod.escape(str(item.get("text", "")))
                # Basename only. The full vault path doubles the line length
                # and wraps the item onto a second row, which is the whole
                # cost this screen is trying not to pay; the file is still
                # identifiable.
                source = str(item.get("source") or "")
                tail = (
                    f' <span style="{_S_TRACE}">{html_mod.escape(source.rsplit("/", 1)[-1])}</span>'
                    if source
                    else ""
                )
                out.append(
                    f'<tr><td style="{_S_LEDGER_DUE}">{_due_cell(item, group.get("tier"))}</td>'
                    f'<td style="{_S_LEDGER_ITEM}">{text}{tail}</td></tr>'
                )
        out.append("</table>")
    warnings = brief.get("warnings") or []
    if warnings:
        rendered = "<br>".join(f"! {html_mod.escape(str(w))}" for w in warnings)
        out.append(
            f'<p style="{_S_SMALL}margin:12px 0 0;color:{_URGENT};">{rendered}</p>'
        )
    out.append("</div>")
    return "\n".join(out)


def _render_updates(updates: list[dict[str, Any]]) -> str:
    out = [f'<h2 style="{_S_H2}">状态更新</h2>', f'<ul style="{_S_UL}">']
    for item in updates:
        label = html_mod.escape(str(item.get("label", "")))
        checked = html_mod.escape(str(item.get("date", "")))
        fields = []
        for key, value in (item.get("values") or {}).items():
            fields.append(f"{html_mod.escape(str(key))}: {inline_html(str(value))}")
        summary = " · ".join(fields)
        path = html_mod.escape(str(item.get("path", "")))
        out.append(
            f'<li style="{_S_LI}"><strong>{label} · {checked}</strong><br>{summary} '
            f'<span style="{_S_CODE}">{path}</span></li>'
        )
    out.append("</ul>")
    return "\n".join(out)


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


def _render_feed_health(feeds: dict[str, Any]) -> str:
    """One line on channel reachability, plus anything that changed by itself.

    Worth a line every day because the number is the routine's own honesty
    check: the news collector ran for six days reporting a dozen items while
    reaching one declared channel out of thirty-eight, and nothing on the face
    of the digest could have told the reader that. Repairs and retirements get
    named because they are changes the reader did not make and would otherwise
    discover only by noticing a source had quietly stopped appearing.
    """
    channels = feeds.get("channels") or {}
    declared = int(channels.get("declared") or 0)
    if not declared:
        return ""
    reached = int(channels.get("reached") or 0)
    retired = int(channels.get("retired") or 0)
    bits = [f"频道 {reached}/{declared}"]
    if retired:
        bits.append(f"{retired} 已退役")
    lines = [f'<p style="{_S_SMALL}margin:0 0 6px;">{" · ".join(bits)}</p>']
    for repair in feeds.get("healed") or []:
        lines.append(
            f'<p style="{_S_SMALL}margin:0 0 3px;color:{_ACCENT};">自愈 '
            f'{html_mod.escape(str(repair.get("label", "")))}: '
            f'{html_mod.escape(str(repair.get("now", "")))}</p>'
        )
    for gone in feeds.get("retired") or []:
        lines.append(
            f'<p style="{_S_SMALL}margin:0 0 3px;color:{_ACCENT};">退役 '
            f'{html_mod.escape(str(gone.get("label", "")))}('
            f'{html_mod.escape(str(gone.get("error", "")))},连续失败达上限)</p>'
        )
    return "".join(lines)


def _render_routine_briefs(
    briefs: list[dict[str, Any]], manifest: dict[str, Any]
) -> str:
    """One short Chinese summary per routine, capped by the registry.

    The cap is enforced here rather than trusted to the writer. A budget that
    depends on the summariser's restraint is not a budget: one verbose collector
    crowds out five terse ones, and the reader pays for it every morning without
    ever seeing why.

    Full reports are still carried below the fold. This section is the decision
    surface: enough to know whether a routine found anything worth descending
    into, and no more.
    """
    caps = {
        s["path"]: int(s.get("max_lines") or DEFAULT_ROUTINE_LINES)
        for _lane, s in iter_sources(manifest)
    }
    labels = {s["path"]: str(s.get("label", "")) for _lane, s in iter_sources(manifest)}
    entries: list[str] = []
    for brief in briefs:
        if not isinstance(brief, dict):
            continue
        path = str(brief.get("path") or "")
        summary = str(brief.get("summary") or "").strip()
        if not summary:
            continue
        cap = caps.get(path, DEFAULT_ROUTINE_LINES)
        lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
        clipped = len(lines) > cap
        lines = lines[:cap]
        label = html_mod.escape(labels.get(path) or Path(path).name)
        body = "<br>".join(inline_html(ln) for ln in lines)
        tail = (
            f'<span style="{_S_TRACE}"> 已截至 {cap} 行</span>' if clipped else ""
        )
        entries.append(
            f'<div style="{_S_ART}">'
            f'<p style="{_S_DEEP_HEAD}">{label}{tail}</p>'
            f'<p style="{_S_ART_ABSTRACT}">{body}</p></div>'
        )
    return "".join(entries)


def _render_articles(articles: list[dict[str, Any]]) -> str:
    """Saved reading, many entries with a real abstract each.

    Breadth over depth on purpose. Attaching article bodies was measured at
    ~110,000 characters for three pieces, five times the whole depth budget and
    past the size where a mail client clips the message. More pieces with a
    genuine abstract each gives a larger surface to choose from at a fraction of
    the cost, and the Reader link is one tap away for anything that earns it.

    An abstract is required. A bare title is not a decision aid: it asks the
    reader to open the piece to find out whether opening it was worth it, which
    is the tax this section exists to remove.
    """
    entries: list[str] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = html_mod.escape(str(article.get("title") or "")).strip()
        abstract = str(article.get("abstract") or "").strip()
        if not title or not abstract:
            continue
        url = str(article.get("url") or "")
        head = (
            f'<a href="{html_mod.escape(url)}" style="{_S_LINK}">{title}</a>'
            if url
            else title
        )
        meta_bits = []
        if article.get("minutes"):
            meta_bits.append(f'{html_mod.escape(str(article["minutes"]))} min')
        if article.get("source"):
            meta_bits.append(html_mod.escape(str(article["source"])))
        meta = (
            f'<span style="{_S_ART_META}">{" · ".join(meta_bits)}</span>'
            if meta_bits
            else ""
        )
        why = str(article.get("why") or "").strip()
        style = _S_ART_FIRST if not entries else _S_ART
        entry = [f'<div style="{style}">', f'<p style="{_S_ART_TITLE}">{head}</p>{meta}']
        if why:
            entry.append(f'<p style="{_S_ART_WHY}">{inline_html(why)}</p>')
        entry.append(f'<p style="{_S_ART_ABSTRACT}">{html_mod.escape(abstract)}</p>')
        entry.append("</div>")
        entries.append("".join(entry))
    return "".join(entries)


def _articles_badge(articles: list[dict[str, Any]]) -> str:
    """Count and total minutes of the entries the section will actually show."""
    shown = [
        a
        for a in articles
        if isinstance(a, dict) and str(a.get("title") or "").strip() and str(a.get("abstract") or "").strip()
    ]
    if not shown:
        return ""
    minutes = 0
    for article in shown:
        try:
            minutes += int(article.get("minutes") or 0)
        except (TypeError, ValueError):
            pass
    bits = [f"{len(shown)} 篇"] + ([f"{minutes} 分钟"] if minutes else [])
    return f'<span style="{_S_H2_N}">{" · ".join(bits)}</span>'


def _within_days(day: str, reference: str, days: int) -> bool:
    try:
        first = date.fromisoformat(day[:10])
        second = date.fromisoformat(reference[:10])
    except ValueError:
        return False
    return 0 <= (second - first).days <= days


def _render_frontier(labs: Any, digest_date: str) -> str:
    """The frontier-lab sweep as one section: signals, drift, promotions.

    The sweep is weekly and the digest is daily, so the full table renders only
    on the sweep's own day and the day after; later days keep the heading and
    its counts, which is enough to know nothing new arrived without paying the
    first screen for a week-old table.
    """
    if not isinstance(labs, dict):
        return ""
    signals = [
        s
        for s in labs.get("signals") or []
        if isinstance(s, dict) and s.get("lab") and s.get("text")
    ]
    drift = int(labs.get("drift_count") or 0)
    promotions = int(labs.get("promotion_count") or 0)
    sweep = str(labs.get("sweep_date") or "")
    badge_bits = [f"{len(signals)} 条信号", f"{drift} 漂移", f"{promotions} 晋级"]
    if sweep:
        badge_bits.append(f"周扫 {sweep[5:] or sweep}")
    heading = (
        f'<h2 style="{_S_H2}">前沿实验室'
        f'<span style="{_S_H2_N}">{html_mod.escape(" · ".join(badge_bits))}</span></h2>'
    )
    if not signals and not labs.get("watchlist_note"):
        return heading
    # Unknown digest date fails closed: a table of unknown age is the thing
    # this rule exists to keep off the first screen.
    if sweep and (not digest_date or not _within_days(sweep, digest_date, 1)):
        return heading + (
            f'<p style="{_S_SMALL}">本期周扫 {html_mod.escape(sweep)} 已随当日 digest 报告，'
            "本周尚无新扫描。</p>"
        )
    rows = []
    for signal in signals:
        cat = html_mod.escape(str(signal.get("category") or ""))
        tier = signal.get("tier")
        if tier:
            tier_label = f"{html_mod.escape(str(tier))}级来源"
            cat = f"{cat} · {tier_label}" if cat else tier_label
        link = (
            f' <a href="{html_mod.escape(str(signal["url"]))}" style="{_S_SRC_LINK}">来源 ↗</a>'
            if signal.get("url")
            else ""
        )
        rows.append(
            f'<tr><td style="{_S_LAB_NAME}"><span style="font-weight:600;">'
            f'{html_mod.escape(str(signal["lab"]))}</span>'
            + (f'<br><span style="{_S_LAB_CAT}">{cat}</span>' if cat else "")
            + f'</td><td style="{_S_LAB_TEXT}">{inline_html(str(signal["text"]))}{link}</td></tr>'
        )
    note = str(labs.get("watchlist_note") or "").strip()
    if note:
        rows.append(f'<tr><td colspan="2" style="{_S_LAB_NOTE}">{inline_html(note)}</td></tr>')
    return heading + f'<table role="presentation" style="{_S_LAB_TABLE}">{"".join(rows)}</table>'


def _render_deep_read_curated(deep_read: Any) -> str:
    """Model-picked findings, rewritten for the reader: two facts and a why.

    No frontmatter, no coverage tails, no effort reports. Those belong to the
    routine's own file, which the source index still points at.
    """
    if not isinstance(deep_read, dict):
        return ""
    entries = [
        e
        for e in deep_read.get("entries") or []
        if isinstance(e, dict) and e.get("title") and (e.get("facts") or e.get("why"))
    ]
    if not entries:
        return ""
    total = deep_read.get("total")
    label = f"信号精选 · {len(entries)}" + (f" / {total}" if total else "")
    rows = [f'<tr><td style="{_S_DR_GROUP}">{html_mod.escape(label)}</td></tr>']
    for entry in entries:
        link = (
            f' <a href="{html_mod.escape(str(entry["url"]))}" style="{_S_SRC_LINK}">来源 ↗</a>'
            if entry.get("url")
            else ""
        )
        body = f'<p style="{_S_DR_TITLE}">{html_mod.escape(str(entry["title"]))}{link}</p>'
        facts = [str(f) for f in (entry.get("facts") or []) if str(f).strip()][:2]
        if facts:
            body += f'<ul style="{_S_DR_UL}">' + "".join(
                f'<li style="{_S_DR_LI}">{inline_html(f)}</li>' for f in facts
            ) + "</ul>"
        why = str(entry.get("why") or "").strip()
        if why:
            body += f'<p style="{_S_DR_WHY}">{inline_html(why)}</p>'
        rows.append(f'<tr><td style="{_S_DR_ITEM}">{body}</td></tr>')
    return f'<table role="presentation" style="{_S_LAB_TABLE}">{"".join(rows)}</table>'


def _render_feed_items(manifest: dict[str, Any]) -> str:
    """The tech feed's items as one list: title link and its one-line note.

    Deterministic, from the manifest. The feed routine already writes the note
    in the reader's language, and its cluster/mode/provenance tail is metadata
    the reader asked not to see.
    """
    rows = []
    for lane, source in iter_sources(manifest):
        if lane != "Tech feed":
            continue
        for item in source.get("items") or []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            url = str(item.get("url") or "")
            head = (
                f'<a href="{html_mod.escape(url)}" style="{_S_LINK}font-weight:600;">'
                f"{html_mod.escape(title)}</a>"
                if url
                else f'<span style="font-weight:600;">{html_mod.escape(title)}</span>'
            )
            note = str(item.get("note") or "").strip()
            tail = f'<br><span style="color:{_MUTED};">{html_mod.escape(note)}</span>' if note else ""
            rows.append(
                f'<tr><td style="padding:9px 0;border-top:1px solid {_RULE};">{head}{tail}</td></tr>'
            )
    if not rows:
        return ""
    return (
        f'<table role="presentation" style="{_S_LAB_TABLE}">'
        f'<tr><td style="{_S_DR_GROUP}">科技动态 · {len(rows)}</td></tr>'
        f'{"".join(rows)}</table>'
    )


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


def _deep_read_badge(deep_read: Any, manifest: dict[str, Any]) -> str:
    bits = []
    if isinstance(deep_read, dict):
        entries = deep_read.get("entries") or []
        total = deep_read.get("total")
        if entries:
            bits.append(f"信号 {len(entries)}" + (f" / {total}" if total else ""))
    feed = sum(
        len(source.get("items") or [])
        for lane, source in iter_sources(manifest)
        if lane == "Tech feed"
    )
    if feed:
        bits.append(f"科技动态 {feed}")
    return f'<span style="{_S_H2_N}">{html_mod.escape(" · ".join(bits))}</span>' if bits else ""


def _render_section(section: dict[str, Any], manifest: dict[str, Any]) -> str:
    if not isinstance(section, dict):
        # Model-written JSON: a bare string where an object belongs is a
        # plausible authoring slip, and one bad section must not cost the
        # whole document.
        return f'<p style="{_S_SMALL}">(malformed overview section skipped)</p>'
    known_paths = {s["path"] for _, s in iter_sources(manifest)}
    bullets = section.get("bullets") or []
    out = [
        f'<h2 style="{_S_H2}">{inline_html(section.get("title", ""))}'
        f'<span style="{_S_H2_N}">{len(bullets)}</span></h2>'
    ]
    if section.get("note"):
        out.append(f'<p style="{_S_P}">{inline_html(section["note"])}</p>')
    if bullets:
        out.append(f'<ul style="{_S_UL}">')
        for bullet in bullets:
            out.append(f'<li style="{_S_LI}">{_render_bullet(bullet, known_paths)}</li>')
        out.append("</ul>")
    return "\n".join(out)


def _render_bullet(bullet: Any, known_paths: set[str]) -> str:
    """One overview bullet with its provenance tail.

    Source references render as plain labels, never as `#anchor` links into the
    source index below. The document's primary destination is an email client,
    and Gmail rewrites in-message anchors so they navigate nowhere; a link that
    looks clickable and does nothing is worse than a label. The `id` attributes
    stay on the index entries for the case where the artifact is opened in a
    browser directly.

    `known_paths` still matters: a reference that does not match a manifest path
    is a sign the overview cited something it did not read, so it renders
    unmarked rather than silently looking sourced.
    """
    if not isinstance(bullet, dict):
        return inline_html(bullet)
    body = inline_html(bullet.get("text", ""))
    tail: list[str] = []
    if bullet.get("url"):
        tail.append(
            f'<a href="{html_mod.escape(str(bullet["url"]))}" '
            f'style="{_S_LINK}">source</a>'
        )
    for ref in bullet.get("sources") or []:
        ref = str(ref)
        label = html_mod.escape(Path(ref).name)
        tail.append(
            f'<code style="{_S_CODE}">{label}</code>'
            if ref in known_paths
            else f"{label} (unmatched)"
        )
    if tail:
        body += f' <span style="{_S_SMALL}">{" · ".join(tail)}</span>'
    return body


# Depth budget. 45 minutes is the middle of the agreed 30-60, and the byte
# ceiling keeps the same render under Gmail's ~102 KB clip so the artifact and
# the mail stay byte-identical: one render reaching two destinations is what
# makes the mail a presentation of the source of truth rather than a second
# document.
DEEP_TARGET_MINUTES = 45
DEEP_PER_SOURCE_CHARS = 6000
DEEP_TOTAL_BYTES = 90_000

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MD_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_MD_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_MD_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def markdown_to_html(text: str, *, limit: int = DEEP_PER_SOURCE_CHARS) -> tuple[str, bool]:
    """A deliberately small Markdown subset, escaped first so content stays inert.

    Routine output is data written by scheduled agents, never instruction, so
    every line is HTML-escaped before any pattern runs. What survives is the
    subset those reports actually use: headings, paragraphs, lists, tables,
    links, bold and code. Everything else degrades to text rather than being
    dropped, because a report that renders imperfectly is still readable and a
    report with a silently missing section is not.

    Images are removed outright: mail clients block remote images by default,
    so they cost bytes and a broken-image box and return nothing.

    Returns (html, truncated).
    """
    body = _MD_IMAGE.sub("", text)
    truncated = len(body) > limit
    if truncated:
        cut = body[:limit]
        newline = cut.rfind("\n")
        body = cut[:newline] if newline > limit * 0.6 else cut

    out: list[str] = []
    list_open = False
    table: list[list[str]] = []

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    def flush_table() -> None:
        nonlocal table
        if not table:
            return
        head, *rest = table
        out.append(f'<table style="{_S_MD_TABLE}">')
        out.append(
            "<tr>" + "".join(f'<th style="{_S_MD_TH}">{c}</th>' for c in head) + "</tr>"
        )
        for row in rest:
            out.append(
                "<tr>" + "".join(f'<td style="{_S_MD_TD}">{c}</td>' for c in row) + "</tr>"
            )
        out.append("</table>")
        table = []

    in_fence = False
    for raw in body.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            close_list()
            flush_table()
            continue

        if _MD_TABLE_RULE.match(line):
            continue
        table_row = _MD_TABLE_ROW.match(line)
        if table_row:
            close_list()
            table.append([inline_html(c.strip()) for c in table_row.group(1).split("|")])
            continue
        flush_table()

        heading = _MD_HEADING.match(line)
        if heading:
            close_list()
            level = min(len(heading.group(1)), 4)
            style = _S_MD_H3 if level >= 3 else _S_MD_H2
            out.append(f'<p style="{style}">{inline_html(heading.group(2))}</p>')
            continue

        item = _MD_LIST.match(line)
        if item:
            if not list_open:
                out.append(f'<ul style="{_S_MD_UL}">')
                list_open = True
            out.append(f'<li style="{_S_MD_LI}">{inline_html(item.group(1))}</li>')
            continue

        close_list()
        out.append(f'<p style="{_S_MD_P}">{inline_html(line.strip())}</p>')

    close_list()
    flush_table()
    return "".join(out), truncated


def _render_deep_read(manifest: dict[str, Any], ov: Path | None = None) -> str:
    """The routine reports in full, so the reader never has to open a file.

    The source index is navigation and stays navigation. This is the body, and
    it is what earns the document its length: the whole report, not the 800
    characters the manifest keeps for whoever writes the overview.

    Bodies are read from disk rather than taken from the manifest because the
    manifest projection is bounded on purpose and shrinking it further to fit
    here would defeat both uses. When $OV is unavailable the manifest excerpt is
    the fallback, which is worse but never wrong.
    """
    entries: list[str] = []
    budget = DEEP_TOTAL_BYTES
    for _lane, source in iter_sources(manifest):
        label = html_mod.escape(str(source.get("label", "")))
        when = html_mod.escape(str(source.get("date", "")))
        headline = html_mod.escape(str(source.get("headline") or source.get("label", "")))

        body_html = ""
        truncated = False
        if ov is not None:
            path = ov / str(source.get("path", ""))
            try:
                body_html, truncated = markdown_to_html(
                    path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                body_html = ""
        if not body_html:
            excerpt = str(source.get("excerpt") or "").strip()
            if not excerpt:
                continue
            body_html = f'<p style="{_S_MD_P}">{html_mod.escape(excerpt)}</p>'

        entry = (
            f'<div style="{_S_DEEP_ITEM}">'
            f'<p style="{_S_DEEP_HEAD}">{headline}'
            f'<span style="{_S_RETRO_META}">{label} · {when}</span></p>'
            f"{body_html}"
        )
        if truncated:
            entry += (
                f'<p style="{_S_SMALL}margin:2px 0 0;">'
                f"报告在此截断,完整版在 <code style=\"{_S_CODE}\">"
                f'{html_mod.escape(str(source.get("path", "")))}</code></p>'
            )
        entry += "</div>"

        size = len(entry.encode("utf-8"))
        if size > budget:
            entries.append(
                f'<p style="{_S_NOTE}">余下的报告未随信附上,以免整封被邮件客户端截断。'
                "见来源索引。</p>"
            )
            break
        budget -= size
        entries.append(entry)
    return "".join(entries)


def _render_retrospect(picks: list[dict[str, Any]]) -> str:
    """Old notes, resurfaced. Only ones a reviewer cleared for mail.

    The filter is applied here as well as at draw time. Delivery is the last
    place the rule can still be enforced, and an unreviewed pick reaching this
    function at all would mean something upstream regressed.
    """
    entries: list[str] = []
    for pick in picks:
        if not pick.get("reviewed"):
            continue
        excerpt = str(pick.get("excerpt") or "").strip()
        if not excerpt:
            continue
        days = int(pick.get("age_days") or 0)
        age = f"{days / 365:.1f} 年前" if days >= 365 else f"{days} 天前"
        entries.append(
            f'<div style="{_S_DEEP_ITEM}">'
            f'<p style="{_S_DEEP_HEAD}">{html_mod.escape(str(pick.get("title", "")))}'
            f'<span style="{_S_RETRO_META}">{html_mod.escape(age)} · '
            f'{html_mod.escape(str(pick.get("tier", "")))}</span></p>'
            f'<p style="{_S_DEEP_BODY}">{html_mod.escape(excerpt)}</p>'
            f'<p style="{_S_SMALL}margin:5px 0 0;">'
            f'<code style="{_S_CODE}">{html_mod.escape(str(pick.get("path", "")))}</code>'
            "</p></div>"
        )
    return "".join(entries)


def _render_source(source: dict[str, Any]) -> str:
    """One compact source-index entry.

    Excerpts stay in the manifest, not here. The manifest exists to be read by
    whoever writes the overview; this document is the summary-depth artifact,
    and pasting every unit excerpt into it would turn an eight-minute read back
    into an hour. What earns its place here is identity (routine, date,
    headline), navigation (slugs, item links, primary URLs), and provenance
    (the vault-relative path). A file-level excerpt appears only when nothing
    else would tell the reader what the output was about.
    """
    anchor = html_mod.escape(str(source.get("anchor", "")))
    label = html_mod.escape(str(source.get("label", "")))
    when = html_mod.escape(str(source.get("date", "")))
    headline = html_mod.escape(str(source.get("headline", "")))
    head = f"<strong>{label}</strong> · {when}"
    if headline:
        head += f" · {headline}"
    if source.get("date_source"):
        head += f' <small>(date from {html_mod.escape(str(source["date_source"]))})</small>'
    lines = [f'<li id="{anchor}" style="{_S_INDEX_LI}">{head}']

    units = source.get("units") or []
    items = source.get("items") or []
    urls = source.get("primary_urls") or []

    if units:
        rendered = []
        for unit in units:
            slug = html_mod.escape(humanize_slug(str(unit.get("slug", ""))))
            url = str(unit.get("source_url", ""))
            rendered.append(
                f'<a href="{html_mod.escape(url)}" style="{_S_LINK}">{slug}</a>'
                if url
                else slug
            )
        lines.append(f'<br>{" · ".join(rendered)}')
    elif items:
        rendered = []
        for item in items[:_INDEX_ITEM_CAP]:
            title = html_mod.escape(str(item.get("title") or item.get("url", "")))
            rendered.append(
                f'<a href="{html_mod.escape(str(item.get("url", "")))}" '
                f'style="{_S_LINK}">{title}</a>'
            )
        tail = f" · +{len(items) - _INDEX_ITEM_CAP} more" if len(items) > _INDEX_ITEM_CAP else ""
        lines.append(f'<br>{" · ".join(rendered)}{tail}')
    elif urls:
        links = " · ".join(
            f'<a href="{html_mod.escape(u)}" style="{_S_LINK}">primary {i + 1}</a>'
            for i, u in enumerate(urls[:2])
        )
        lines.append(f"<br>{links}")
    elif source.get("excerpt"):
        excerpt = str(source["excerpt"])
        if len(excerpt) > _INDEX_EXCERPT_CAP:
            excerpt = excerpt[:_INDEX_EXCERPT_CAP].rstrip() + " …"
        lines.append(f"<br>{html_mod.escape(excerpt)}")

    lines.append(
        f'<br><code style="{_S_CODE}">'
        f'{html_mod.escape(str(source.get("path", "")))}</code></li>'
    )
    return "".join(lines)


# ---------------------------------------------------------------- write


def artifact_name(manifest: dict[str, Any]) -> str:
    window = manifest.get("window", {})
    mode = str(manifest.get("mode", "weekly"))
    return f"{window.get('until', 'unknown')}-{mode}-digest.html"


def resolve_output_dir(ov: Path, routine_name: str = "") -> Routine:
    """Find the digest routine's own row, and refuse to run without an exclusion.

    A digest that ingests its own previous output compounds: yesterday's
    document becomes today's source, and its headline outranks the real
    findings. The registry already has the switch for this (`include = false`),
    so the check is whether it was actually set, not whether we can work around
    it here.

    Without a name, the row is inferred: the registry is private config and the
    routine's name is not exported into the sandbox, so the procedure cannot
    state it. Exactly one row carrying an explicit `include = false` is the
    digest's own; zero or several is an error naming the fix, never a guess.
    """
    routines = load_routines(ov)
    if not routine_name:
        excluded = [routine for routine in routines if routine.excluded_explicitly]
        if len(excluded) == 1:
            return excluded[0]
        if not excluded:
            raise SystemExit(
                "no routine in _meta/routine_watch.toml carries digest = { include = false }; "
                "set it on the digest routine's row or pass --routine"
            )
        names = ", ".join(sorted(routine.name for routine in excluded))
        raise SystemExit(
            f"several routines are excluded from the digest ({names}); "
            "pass --routine to say which one writes it"
        )
    for routine in routines:
        if routine.name != routine_name:
            continue
        if routine.include:
            raise SystemExit(
                f"routine {routine_name!r} writes the digest but is not excluded from it; "
                "set digest = { include = false } on its routine_watch.toml row"
            )
        return routine
    raise SystemExit(f"routine {routine_name!r} has no row in _meta/routine_watch.toml")


def write(
    ov: Path,
    html_text: str,
    manifest: dict[str, Any],
    *,
    routine_name: str = "",
    out: Path | None = None,
    dry_run: bool = False,
) -> int:
    """Write the rendered document into $OV as the run's canonical artifact."""
    if out is None:
        routine = resolve_output_dir(ov, routine_name)
        out = ov / routine.output_dir / artifact_name(manifest)

    size = len(html_text.encode("utf-8"))
    if size > GMAIL_CLIP_BYTES:
        print(
            f"warning: {size / 1024:.0f} KB document; Gmail clips past "
            f"{GMAIL_CLIP_BYTES // 1000} KB and the source index will fold behind "
            "'View entire message'",
            file=sys.stderr,
        )
    if dry_run:
        print(f"would write {fmt(out)} ({size / 1024:.0f} KB)")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, html_text)
    advance_update_state(ov, manifest)
    print(f"wrote {fmt(out)} ({size / 1024:.0f} KB)")
    return 0


# ---------------------------------------------------------------- ack


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
            for path in directory.glob(routine.file_pattern)
            if before < path.name <= after and path.name not in shown
        )
        if count:
            hidden.append((routine.label, count))
    return hidden


def ack(ov: Path, manifest: dict[str, Any], *, dry_run: bool = False) -> int:
    """Advance routine_acks.json past every file this digest covered.

    Same key space and comparison as `cues.py check_routine_outputs`
    ({output_dir: last_acked_filename}, string compare), so acking here is what
    clears the session-start review-debt cue. Never moves an ack backwards.
    """
    targets = manifest.get("acks") or {}
    if not targets:
        print("nothing to ack (empty manifest)")
        return 0
    current = load_acks(ov)
    updated = dict(current)
    changes: list[str] = []
    shown = manifest_names_by_dir(manifest)
    for output_dir, name in sorted(targets.items()):
        before = current.get(output_dir, "")
        if name > before:
            updated[output_dir] = name
            changes.append(f"  {output_dir}: {before or '∅'} → {name}")
            hidden = hidden_by_ack(ov, output_dir, before, name, shown.get(output_dir, set()))
            for label, count in hidden:
                changes.append(
                    f"    warning: also marks {count} unshown {label} file(s) in "
                    f"{output_dir} as reviewed (shared directory, ack is per directory)"
                )
    if not changes:
        print("acks already current")
        return 0
    print("\n".join(changes))
    if dry_run:
        print("(dry run; no write)")
        return 0
    path = ov / "_meta" / "routine_acks.json"
    atomic_write(path, json.dumps(updated, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"updated {fmt(path)} ({len(changes)} directories)")
    return 0


# ---------------------------------------------------------------- mail


MAIL_CONFIG = "_meta/mail.toml"


def load_mail_config(ov: Path) -> dict[str, Any]:
    """SMTP settings from private vault config.

    Deliberately not in the repo and not in the prompt: the recipient is the
    single most important field here, and it must not be somewhere a model can
    reach or a routine prompt can restate. The address lives in config, the
    script reads it, and nothing between the two can redirect a message.

        # $OV/_meta/mail.toml
        [smtp]
        host = "smtp.gmail.com"
        port = 587
        username = "you@example.com"     # also the recipient; this is send-self
        keychain_service = "atelier-smtp"
    """
    path = ov / MAIL_CONFIG
    if not path.is_file():
        raise SystemExit(f"mail config missing: {fmt(path)}")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise SystemExit(f"mail config unreadable: {exc!r}") from exc
    smtp = document.get("smtp")
    if not isinstance(smtp, dict):
        raise SystemExit(f"{fmt(path)} needs an [smtp] table")
    missing = [k for k in ("host", "username") if not smtp.get(k)]
    if missing:
        raise SystemExit(f"{fmt(path)} [smtp] missing: {', '.join(missing)}")
    if not smtp.get("keychain_service") and not smtp.get("password_file"):
        raise SystemExit(
            f"{fmt(path)} [smtp] needs keychain_service, password_file, or both"
        )
    return smtp


KEYCHAIN_TIMEOUT_SECONDS = 10


def _refuse_vault_secret(path: Path) -> None:
    """A password file under $OV is on a synced Drive mount: refuse it."""
    try:
        root = vault_root().resolve()
    except PathsError:
        return
    try:
        inside = path.resolve().is_relative_to(root)
    except OSError:
        return
    if inside:
        raise SystemExit(
            f"{fmt(path)} is under $OV; a password file must live outside the "
            "synced vault (for example ~/.config/atelier/)"
        )


def smtp_password(smtp: dict[str, Any]) -> str:
    """The app password, from the keychain if reachable, else a 0600 file.

    Never an argument, never an environment variable, never in the repo, and
    never under $OV -- $OV is a synced Drive mount, so a secret written there
    leaves the machine and stays in Drive's revision history.

    The keychain is preferred and tried first, but it cannot be relied on alone.
    Inside the routine sandbox the read blocks on an interaction prompt that no
    one will ever answer, which is worse than failing: an unattended job hangs
    until its timeout. So the read is hard-bounded, and a file fallback exists
    for exactly that case. The file is protected by permissions rather than by
    the keychain, which is a real reduction; it is the price of a delivery path
    that works unattended.
    """
    service = str(smtp.get("keychain_service") or "")
    account = str(smtp["username"])
    tried: list[str] = []

    if service:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", service, "-a", account],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            tried.append(f"keychain {service!r}: {result.stderr.strip() or 'not found'}")
        except subprocess.TimeoutExpired:
            tried.append(
                f"keychain {service!r}: timed out after {KEYCHAIN_TIMEOUT_SECONDS}s "
                "(blocked on an interaction prompt this context cannot answer)"
            )

    raw_path = smtp.get("password_file")
    if raw_path:
        path = Path(str(raw_path)).expanduser()
        _refuse_vault_secret(path)
        if path.is_file():
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise SystemExit(
                    f"{fmt(path)} is mode {mode:o}; a password file must be 0600 "
                    "(chmod 600 it)"
                )
            password = path.read_text(encoding="utf-8").strip().replace(" ", "")
            if password:
                return password
            tried.append(f"{fmt(path)}: empty")
        else:
            tried.append(f"{fmt(path)}: missing")

    raise SystemExit(
        "no SMTP password available; tried " + "; ".join(tried or ["nothing configured"])
    )


def build_message(html_text: str, subject: str, sender: str, recipient: str):
    """One multipart/alternative message carrying the artifact verbatim."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    # A short plain-text alternative rather than a stripped rendering: a second
    # wording of the same document is the "parallel summary" the protocol warns
    # about, and every client that matters renders the HTML part.
    message.set_content(
        "This digest is an HTML message. If you are reading this, your client "
        "did not render it; the canonical copy is the artifact in your vault."
    )
    message.add_alternative(html_text, subtype="html")
    return message


def mail(
    ov: Path,
    html_text: str,
    subject: str,
    *,
    dry_run: bool = False,
) -> int:
    """Send the artifact to the configured account, and nowhere else."""
    import smtplib

    smtp = load_mail_config(ov)
    host = str(smtp["host"])
    port = int(smtp.get("port", 587))
    account = str(smtp["username"])
    size = len(html_text.encode("utf-8"))
    if size > GMAIL_CLIP_BYTES:
        print(
            f"warning: {size / 1024:.0f} KB message; Gmail clips past "
            f"{GMAIL_CLIP_BYTES // 1000} KB",
            file=sys.stderr,
        )
    if dry_run:
        print(f"would send {size / 1024:.0f} KB to {account} via {host}:{port}")
        print(f"subject: {subject}")
        return 0

    message = build_message(html_text, subject, account, account)
    password = smtp_password(smtp)
    try:
        with smtplib.SMTP(host, port, timeout=60) as server:
            server.starttls()
            server.login(account, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"send failed: {exc!r}", file=sys.stderr)
        return 1
    print(f"sent {size / 1024:.0f} KB to {account}")
    return 0


# ---------------------------------------------------------------- text report


def text_report(manifest: dict[str, Any]) -> str:
    counts = manifest.get("counts", {})
    window = manifest.get("window", {})
    lines = [
        f"{digest_title(manifest)}",
        f"selection={manifest.get('selection')} window={window.get('since')}..{window.get('until')} "
        f"routines={counts.get('routines', 0)} files={counts.get('files', 0)} "
        f"updates={counts.get('updates', 0)} bytes={counts.get('bytes', 0)}",
    ]
    for lane in manifest.get("lanes", []):
        lines.append(f"\n{lane.get('lane')} ({lane.get('files')})")
        for source in lane.get("sources", []):
            marks = []
            if source.get("units"):
                marks.append(f"{len(source['units'])} units")
            if source.get("items"):
                marks.append(f"{len(source['items'])} items")
            suffix = f" [{', '.join(marks)}]" if marks else ""
            headline = source.get("headline") or "(no headline)"
            lines.append(f"  {source.get('date')}  {source.get('label')}: {headline}{suffix}")
    if manifest.get("updates"):
        lines.append("\nStatus updates")
        for update in manifest["updates"]:
            fields = " · ".join(
                f"{key}: {value}" for key, value in (update.get("values") or {}).items()
            )
            lines.append(f"  {update.get('date')}  {update.get('label')}: {fields}")
    for warning in manifest.get("update_warnings") or []:
        lines.append(f"warning: {warning}")
    if manifest.get("skipped_routines"):
        lines.append(f"\nexcluded: {', '.join(manifest['skipped_routines'])}")
    if manifest.get("truncated"):
        lines.append("truncated by --max-files")
    return "\n".join(lines)


# ---------------------------------------------------------------- cli


def _load_manifest(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"manifest unreadable: {exc!r}") from exc
    if data.get("schema") != MANIFEST_SCHEMA:
        raise SystemExit(f"manifest schema {data.get('schema')} unsupported")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Roll up scheduled-routine outputs into a Readwise document.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect", help="Select routine outputs in a window.")
    p_collect.add_argument("--mode", choices=["daily", "weekly"], default="weekly")
    p_collect.add_argument("--days", type=int, help="Override window length in days.")
    p_collect.add_argument("--since", help="Window start (YYYY-MM-DD).")
    p_collect.add_argument("--until", help="Window end (YYYY-MM-DD); default effective today.")
    p_collect.add_argument(
        "--unacked", action="store_true", help="Ignore the window; take everything past each ack."
    )
    p_collect.add_argument(
        "--include-maintenance", action="store_true", help="Include harness-maintenance routines."
    )
    p_collect.add_argument("--excerpt-chars", type=int, default=DEFAULT_EXCERPT_CHARS)
    p_collect.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    p_collect.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    p_collect.add_argument(
        "--feeds",
        help="feed_fetch.py --json output; its channel counts ride along in the manifest.",
    )
    p_collect.add_argument("--json", action="store_true", help="JSON manifest instead of a report.")
    p_collect.add_argument("--out", help="Write output to a file instead of stdout.")

    p_render = sub.add_parser("render", help="Render a manifest to HTML.")
    p_render.add_argument("--manifest", required=True, help="Manifest path, or - for stdin.")
    p_render.add_argument("--overview", help="Overview JSON written by the /digest command.")
    p_render.add_argument("--brief", help="Action-surface JSON from daily_brief.py --json.")
    p_render.add_argument("--context", help="Masthead JSON from daily_context.py --json.")
    p_render.add_argument(
        "--retrospect",
        help=(
            "Picks from retrospect.py --json. Only entries a reviewer marked "
            "reviewed are rendered; the rest are dropped here as well as at draw time."
        ),
    )
    p_render.add_argument("--out", help="Write HTML to a file instead of stdout.")

    p_write = sub.add_parser(
        "write", help="Render into the routine's declared $OV output directory."
    )
    p_write.add_argument("--manifest", required=True, help="Manifest path, or - for stdin.")
    p_write.add_argument("--overview", help="Overview JSON written by the /digest command.")
    p_write.add_argument("--brief", help="Action-surface JSON from daily_brief.py --json.")
    p_write.add_argument("--context", help="Masthead JSON from daily_context.py --json.")
    p_write.add_argument(
        "--retrospect",
        help=(
            "Picks from retrospect.py --json. Only entries a reviewer marked "
            "reviewed are rendered; the rest are dropped here as well as at draw time."
        ),
    )
    p_write.add_argument(
        "--routine",
        help=(
            "Routine whose output_dir receives the artifact. Defaults to the one "
            "routine_watch.toml row carrying digest = { include = false }."
        ),
    )
    p_write.add_argument("--out", help="Explicit destination, overriding --routine.")
    p_write.add_argument("--dry-run", action="store_true", help="Report the path, write nothing.")

    p_mail = sub.add_parser(
        "mail", help="Send an artifact to the configured account, and nowhere else."
    )
    p_mail.add_argument("--html", required=True, help="Rendered artifact to send.")
    p_mail.add_argument("--subject", required=True, help="Message subject.")
    p_mail.add_argument("--dry-run", action="store_true", help="Report, send nothing.")

    p_ack = sub.add_parser("ack", help="Advance routine_acks.json past digested files.")
    p_ack.add_argument("--manifest", required=True)
    p_ack.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)

    try:
        ov = vault_root()
    except PathsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.cmd == "collect":
        manifest = collect(
            ov,
            mode=args.mode,
            days=args.days,
            since=args.since,
            until=args.until,
            unacked=args.unacked,
            include_maintenance=args.include_maintenance,
            excerpt_chars=args.excerpt_chars,
            max_items=args.max_items,
            max_files=args.max_files,
        )
        if args.feeds:
            try:
                manifest["feeds"] = json.loads(Path(args.feeds).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"feed payload unreadable: {exc!r}", file=sys.stderr)
        payload = (
            json.dumps(manifest, indent=2, ensure_ascii=False)
            if args.json
            else text_report(manifest)
        )
        if args.out:
            Path(args.out).write_text(payload + "\n", encoding="utf-8")
            print(
                f"wrote {args.out} ({manifest['counts']['files']} files, "
                f"{manifest['counts'].get('updates', 0)} updates)"
            )
        else:
            print(payload)
        return 0

    if args.cmd == "render":
        manifest = _load_manifest(args.manifest)
        overview = load_overview(Path(args.overview) if args.overview else None)
        brief = load_brief(Path(args.brief) if args.brief else None)
        picks = load_retrospect(Path(args.retrospect) if args.retrospect else None)
        context = load_context(Path(args.context) if args.context else None)
        document = render(manifest, overview, brief, picks, context)
        if args.out:
            Path(args.out).write_text(document, encoding="utf-8")
            print(f"wrote {args.out} ({len(document.encode('utf-8')) / 1024:.0f} KB)")
        else:
            sys.stdout.write(document)
        return 0

    if args.cmd == "write":
        manifest = _load_manifest(args.manifest)
        counts = manifest.get("counts", {})
        if not counts.get("files") and not counts.get("updates"):
            print("empty window; nothing written")
            return 0
        overview = load_overview(Path(args.overview) if args.overview else None)
        brief = load_brief(Path(args.brief) if args.brief else None)
        picks = load_retrospect(Path(args.retrospect) if args.retrospect else None)
        context = load_context(Path(args.context) if args.context else None)
        gap = deep_read_lane_gap(overview.get("deep_read"), manifest)
        if gap:
            print(f"warning: {gap}", file=sys.stderr)
        return write(
            ov,
            render(manifest, overview, brief, picks, context),
            manifest,
            routine_name=args.routine or "",
            out=Path(args.out) if args.out else None,
            dry_run=args.dry_run,
        )

    if args.cmd == "mail":
        document = Path(args.html).read_text(encoding="utf-8")
        return mail(ov, document, args.subject, dry_run=args.dry_run)

    if args.cmd == "ack":
        manifest = _load_manifest(args.manifest)
        return ack(ov, manifest, dry_run=args.dry_run)

    return 1


if __name__ == "__main__":
    sys.exit(main())
