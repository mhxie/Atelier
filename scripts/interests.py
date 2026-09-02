#!/usr/bin/env python3
"""interests.py: the consumption-driven interest ledger. Stdlib-only.

An interest is anything the user actually consumes: a series, an act, a team,
a book, a game. The ledger records events on it (watched, attended, read,
played, completed, declared) and derives a strength from them, decayed by
time, so a fresh event reinforces the interest and silence lets it fade.
Collection routines read the active set; nobody maintains a list by hand.

Protocol: protocols/interest-discovery.md. Ledger: $OV/_meta/interests.toml.

The script curates; it does not decide. Structured sources (AniList status
changes, the live-events table, Readwise book highlights) become events or
evidence deterministically. Anything that needs a reading, such as which side a
game was about or what a diary sentence meant, is surfaced by `evidence` for
the orchestrator to judge in conversation and record with `add`.

Subcommands:
    list [--json] [--all]           ledger with strength and status
    active [--json] [--kind K]      names routines should search (active + watch)
    evidence [--days N] [--json]    pending rows plus recent diary lines that may describe consumption
    add --name N --kind K [--event E] [--date D] [--source S] [--ref R] [--declared]
    resolve <pending-id>            clear a pending row once it has been judged (recorded or not)
    decline <slug> | undecline <slug>
    ingest [--source anilist|experience-log|readwise|all] [--days N] [--dry-run]
    recompute                       rewrite statuses from strength (ingest does this too)
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PathsError, atomic_write, fmt, vault_root  # noqa: E402

LEDGER = "_meta/interests.toml"
STATE = "_meta/interests_state.json"
TRACKING_SOURCES = "_meta/brief_sources.toml"
DIGEST_CONFIG = "_meta/digest.toml"  # [interests] experience_log = "<vault-relative path>"
DAILY_NOTES = "daily-notes"

KINDS = ("anime", "artist", "team", "player", "book", "game", "show", "festival", "convention", "other")
EVENT_WEIGHTS = {
    "attended": 3.0,
    "completed": 2.0,
    "watched": 1.0,
    "read": 1.0,
    "played": 1.0,
    "listened": 1.0,
    "started": 1.0,
    "declared": 1.5,
    "accepted": 1.0,
}
HALF_LIFE_DAYS = 90.0
ACTIVE_AT = 0.75  # one fresh consumption event is enough to collect for
WATCH_AT = 0.25
# One event per (interest, kind, source) inside this many days: the AniList
# cache reports the same COMPLETED status on every refresh.
DEDUPE_DAYS = 30

# Live-events log categories → interest kind and event kind. Categories whose
# title names the reason for going (an act, a festival, a show) attribute
# themselves. A game does not: "A vs B" says nothing about which side, or
# which player, or whether the point was the playoff atmosphere, and the
# player the user came for may not have played. Those rows go to the pending
# queue unless the title contains a name the ledger already knows.
CATEGORY_MAP = {
    "concert": ("artist", "attended"),
    "音乐节": ("festival", "attended"),
    "festival": ("festival", "attended"),
    "theatre": ("show", "attended"),
    "theater": ("show", "attended"),
    "show": ("show", "attended"),
    "convention": ("convention", "attended"),
}
PENDING_CATEGORIES = {"nba", "nfl", "mlb", "nhl", "mls", "game", "match", "球赛", "比赛", "sports", "sport"}

# Diary lines are never parsed into events here. A bounded cue list only picks
# candidate lines for the orchestrator to read; the reading is the model's.
_NOTE_CUES = (
    "看了", "看完", "追完", "在看", "听了", "去了", "读了", "读完", "在读", "玩了", "在玩", "通关",
    "演唱会", "live", "concert", "比赛", "球赛", "季后赛", "电影", "动漫", "番", "游戏", "书", "小说",
    "watched", "played", "finished", "went to", "listened", "reading",
)
_NOTE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------- model


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).strip().lower()
    text = re.sub(r"[^\w㐀-鿿぀-ヿ]+", "-", text)
    return text.strip("-") or "unnamed"


@dataclass
class Event:
    date: str
    kind: str
    source: str = "manual"
    ref: str = ""

    def weight(self, today: date) -> float:
        try:
            when = date.fromisoformat(self.date[:10])
        except ValueError:
            return 0.0
        age = max(0, (today - when).days)
        return EVENT_WEIGHTS.get(self.kind, 1.0) * math.pow(0.5, age / HALF_LIFE_DAYS)


@dataclass
class Pending:
    id: str
    date: str
    title: str
    category: str = ""
    source: str = "experience-log"
    ref: str = ""


@dataclass
class Interest:
    slug: str
    name: str
    kind: str = "other"
    aliases: list[str] = field(default_factory=list)
    declared: bool = False
    status: str = "watch"
    notes: str = ""
    events: list[Event] = field(default_factory=list)

    def strength(self, today: date) -> float:
        return round(sum(e.weight(today) for e in self.events), 3)

    def last_event(self) -> str:
        return max((e.date for e in self.events), default="")

    def derived_status(self, today: date) -> str:
        if self.status == "declined":
            return "declined"
        strength = self.strength(today)
        if strength >= ACTIVE_AT:
            return "active"
        if strength >= WATCH_AT or self.declared:
            return "watch"
        return "dormant"

    def has_recent(self, kind: str, source: str, day: date, window: int = DEDUPE_DAYS) -> bool:
        for e in self.events:
            if e.kind != kind or e.source != source:
                continue
            try:
                when = date.fromisoformat(e.date[:10])
            except ValueError:
                continue
            if abs((day - when).days) <= window:
                return True
        return False


# ---------------------------------------------------------------- ledger io


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def dump_ledger(interests: list[Interest], pending: list[Pending] | None = None) -> str:
    """A small TOML emitter for this one schema; the stdlib reads TOML but
    does not write it."""
    out = [
        "# Interest ledger. Written by scripts/interests.py; edit by hand only to",
        "# correct a name, add an alias, or set status = \"declined\".",
        "schema = 1",
        "",
    ]
    for pd in sorted(pending or [], key=lambda x: (x.date, x.id)):
        out.append("[[pending]]")
        out.append(f"id = {_toml_str(pd.id)}")
        out.append(f"date = {_toml_str(pd.date)}")
        out.append(f"title = {_toml_str(pd.title)}")
        if pd.category:
            out.append(f"category = {_toml_str(pd.category)}")
        out.append(f"source = {_toml_str(pd.source)}")
        if pd.ref:
            out.append(f"ref = {_toml_str(pd.ref)}")
        out.append("")
    for it in sorted(interests, key=lambda i: i.slug):
        out.append("[[interest]]")
        out.append(f"slug = {_toml_str(it.slug)}")
        out.append(f"name = {_toml_str(it.name)}")
        out.append(f"kind = {_toml_str(it.kind)}")
        if it.aliases:
            out.append("aliases = [" + ", ".join(_toml_str(a) for a in it.aliases) + "]")
        out.append(f"declared = {'true' if it.declared else 'false'}")
        out.append(f"status = {_toml_str(it.status)}")
        if it.notes:
            out.append(f"notes = {_toml_str(it.notes)}")
        for e in sorted(it.events, key=lambda e: e.date):
            out.append("")
            out.append("[[interest.event]]")
            out.append(f"date = {_toml_str(e.date)}")
            out.append(f"kind = {_toml_str(e.kind)}")
            out.append(f"source = {_toml_str(e.source)}")
            if e.ref:
                out.append(f"ref = {_toml_str(e.ref)}")
        out.append("")
    return "\n".join(out)


def load_pending(path: Path) -> list[Pending]:
    if not path.is_file():
        return []
    import tomllib

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        Pending(
            str(row.get("id", "")),
            str(row.get("date", "")),
            str(row.get("title", "")),
            str(row.get("category", "")),
            str(row.get("source", "experience-log")),
            str(row.get("ref", "")),
        )
        for row in data.get("pending") or []
        if row.get("id")
    ]


def load_ledger(path: Path) -> list[Interest]:
    if not path.is_file():
        return []
    import tomllib

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[Interest] = []
    for row in data.get("interest") or []:
        events = [
            Event(str(e.get("date", "")), str(e.get("kind", "")), str(e.get("source", "manual")), str(e.get("ref", "")))
            for e in row.get("event") or []
        ]
        out.append(
            Interest(
                slug=str(row.get("slug") or slugify(str(row.get("name", "")))),
                name=str(row.get("name", "")),
                kind=str(row.get("kind", "other")),
                aliases=[str(a) for a in row.get("aliases") or []],
                declared=bool(row.get("declared", False)),
                status=str(row.get("status", "watch")),
                notes=str(row.get("notes", "")),
                events=events,
            )
        )
    return out


def mentioned_in(interests: list[Interest], title: str) -> list[Interest]:
    """Interests whose name or an alias appears in the title, longest names
    first so "Example Team" beats "Team"."""
    lowered = title.lower()
    hits = []
    for it in interests:
        for label in [it.name, *it.aliases]:
            label = label.strip().lower()
            if len(label) >= 3 and label in lowered:
                hits.append(it)
                break
    return sorted(hits, key=lambda i: -len(i.name))


def find(interests: list[Interest], name: str) -> Interest | None:
    slug = slugify(name)
    lowered = name.strip().lower()
    for it in interests:
        if it.slug == slug or it.name.lower() == lowered or lowered in (a.lower() for a in it.aliases):
            return it
    return None


def upsert_event(
    interests: list[Interest],
    name: str,
    kind: str,
    event: Event,
    *,
    declared: bool = False,
    dedupe: bool = True,
) -> tuple[Interest, bool]:
    """Add an event, creating the interest if needed. Returns (interest, added)."""
    it = find(interests, name)
    if it is None:
        it = Interest(slug=slugify(name), name=name.strip(), kind=kind, declared=declared)
        interests.append(it)
    elif declared:
        it.declared = True
    day = date.fromisoformat(event.date[:10])
    if dedupe and it.has_recent(event.kind, event.source, day):
        return it, False
    it.events.append(event)
    return it, True


def recompute(interests: list[Interest], today: date) -> None:
    for it in interests:
        it.status = it.derived_status(today)


# ---------------------------------------------------------------- ingest


def _load_state(ov: Path) -> dict[str, Any]:
    path = ov / STATE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(ov: Path, state: dict[str, Any]) -> None:
    atomic_write(ov / STATE, json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))


def ingest_anilist(ov: Path, interests: list[Interest], today: date, state: dict[str, Any] | None = None) -> list[str]:
    """Events come from *changes* in the library, not from its contents.

    The cache carries no completion dates, so a first pass that turned every
    COMPLETED row into a completion dated today would flood the ledger with a
    decade of finished series. Instead the first pass records a baseline
    (CURRENT rows with progress become watches, since they are being watched
    now), and later passes emit: PLANNING→CURRENT started, progress up
    watched, →COMPLETED completed. `state` persists between runs.
    """
    import tomllib

    config = ov / TRACKING_SOURCES
    if not config.is_file():
        return ["anilist: no tracking config"]
    try:
        cache_path = tomllib.loads(config.read_text(encoding="utf-8"))["tracking"]["cache"]
    except (KeyError, ValueError):
        return ["anilist: tracking config lacks [tracking] cache"]
    cache_file = Path(str(cache_path)).expanduser()
    if not cache_file.is_absolute():
        cache_file = ov / cache_file
    if not cache_file.is_file():
        return [f"anilist: cache missing at {fmt(cache_file)}"]
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"anilist: cache unreadable: {exc!r}"]
    library = ((cache.get("anime") or {}).get("library")) or []
    stamp = str((cache.get("anime") or {}).get("last_success_at") or cache.get("refreshed_at") or today.isoformat())[:10]
    if state is None:
        state = {}
    previous: dict[str, Any] = state.get("anilist") or {}
    baseline = not previous
    current: dict[str, Any] = {}
    added = 0
    for item in library:
        title = str(item.get("title") or "").strip()
        status = str(item.get("status") or "").upper()
        progress = int(item.get("progress") or 0)
        key = str(item.get("id") or slugify(title))
        if not title:
            continue
        current[key] = {"status": status, "progress": progress, "title": title}
        before = previous.get(key)
        kind = ""
        if baseline:
            if status == "CURRENT" and progress > 0:
                kind = "watched"
        elif before is None:
            if status == "CURRENT" and progress > 0:
                kind = "started"
            elif status == "COMPLETED":
                kind = "completed"
        else:
            if status == "COMPLETED" and before.get("status") != "COMPLETED":
                kind = "completed"
            elif status == "CURRENT" and progress > int(before.get("progress") or 0):
                kind = "watched" if before.get("status") == "CURRENT" else "started"
        if not kind:
            continue
        _, ok = upsert_event(interests, title, "anime", Event(stamp, kind, "anilist", f"anilist:{key}"))
        added += int(ok)
    state["anilist"] = current
    note = "baseline recorded, " if baseline else ""
    return [f"anilist: {note}{added} new events from {len(library)} library entries"]


_ACT_SUFFIX = re.compile(
    r"\s*(?:的)?(?:个人|世界|巡回|全球|北美|亚洲)*\s*(?:演唱会|演出|音乐会|live|concert|tour|world tour)(?!\w).*$",
    re.I,
)
_ACT_SPLIT = re.compile(r"\s*(?::|：|\s-\s|\s–\s|\s—\s|\|)\s*")


def act_name(event: str) -> str:
    """The act behind an event title: 'X: Y Tour' → 'X', 'X 个人演唱会' → 'X'.

    Attended events are recorded by their title, but the interest is the act;
    without this the ledger would carry one entry per tour name and never
    merge with the act the user declares."""
    head = _ACT_SPLIT.split(event.strip(), maxsplit=1)[0]
    head = _ACT_SUFFIX.sub("", head).strip(" 《》「」\"'")
    return head or event.strip()


_EXP_ROW = re.compile(r"^\|\s*(?P<date>[^|]*)\|\s*(?P<event>[^|]+)\|\s*(?P<city>[^|]*)\|\s*(?P<category>[^|]*)\|")
_EXP_DATE = re.compile(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")
_EXP_SECTION = "## Live Events"


def _row_date(cell: str) -> str | None:
    """The date in a table cell that may be a markdown link to the daily
    note, or year-month, or year only; missing parts land on the first."""
    m = _EXP_DATE.search(cell)
    if not m:
        return None
    year, month, day = m.group(1), m.group(2) or "01", m.group(3) or "01"
    when = f"{year}-{month}-{day}"
    try:
        date.fromisoformat(when)
    except ValueError:
        return None
    return when


def experience_log_path(ov: Path) -> Path | None:
    """The user's live-events table, declared privately so its name never
    enters the repo."""
    config = ov / DIGEST_CONFIG
    if not config.is_file():
        return None
    import tomllib

    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rel = str(((data.get("interests") or {}).get("experience_log")) or "").strip()
    return ov / rel if rel else None


def ingest_experience_log(
    ov: Path, interests: list[Interest], today: date, pending: list[Pending] | None = None
) -> list[str]:
    """Rows of the user's own live-events table (Date | Event | City |
    Category | ...). A year-only date lands on the first of that year, which
    decays it honestly. Game rows attribute themselves only to names the
    ledger already knows; otherwise they wait in `pending` for the user."""
    path = experience_log_path(ov)
    if path is None:
        return ["experience-log: not configured ([interests] experience_log in _meta/digest.toml)"]
    if not path.is_file():
        return [f"experience-log: file missing at {fmt(path)}"]
    if pending is None:
        pending = []
    added = 0
    rows = 0
    queued = 0
    skipped = 0
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line.strip() == _EXP_SECTION
            continue
        if not in_section:
            continue
        m = _EXP_ROW.match(line)
        if not m:
            continue
        title = re.sub(r"\s+", " ", m.group("event")).strip()
        category = m.group("category").strip().lower()
        if not title or title.lower() in ("event", "---") or set(title) <= set("-: "):
            continue
        rows += 1
        when = _row_date(m.group("date"))
        if when is None:
            skipped += 1
            continue
        if category not in CATEGORY_MAP and category not in PENDING_CATEGORIES:
            skipped += 1
            continue
        ref = f"experience-log:{when}:{slugify(title)}"
        if category in PENDING_CATEGORIES:
            known = mentioned_in(interests, title)
            if known:
                for it in known:
                    if not any(e.ref == ref for e in it.events):
                        it.events.append(Event(when, "attended", "experience-log", ref))
                        added += 1
            elif not any(pd.ref == ref for pd in pending):
                pending.append(Pending(slugify(f"{when}-{title}")[:60], when, title, category, "experience-log", ref))
                queued += 1
            continue
        kind, event_kind = CATEGORY_MAP[category]
        name = act_name(title) if kind == "artist" else title
        _, ok = upsert_event(interests, name, kind, Event(when, event_kind, "experience-log", ref), dedupe=False)
        # dedupe=False above, but the same (date, ref) must not double on re-ingest:
        it = find(interests, name)
        if it and ok and sum(1 for e in it.events if e.ref == ref and e.source == "experience-log") > 1:
            it.events.pop()
            ok = False
        added += int(ok)
    note = f"experience-log: {added} new events from {rows} rows"
    if queued:
        note += f", {queued} queued for attribution (interests.py pending)"
    if skipped:
        note += f", {skipped} skipped (no date or a category that is not an interest)"
    return [note]


def note_candidates(ov: Path, today: date, days: int) -> list[dict[str, str]]:
    """Recent diary lines that might describe consumption, for a reader.

    A cue match is not an event: 去了 a restaurant, 看了 a document, 玩了 an
    afternoon. The list bounds what the orchestrator has to read; deciding
    what, if anything, each line says about an interest is the model's job.
    """
    root = ov / DAILY_NOTES
    if not root.is_dir():
        return []
    since = today - timedelta(days=days)
    out: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.md")):
        m = _NOTE_DATE.search(path.name)
        if not m:
            continue
        try:
            when = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if when < since or when > today:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            lowered = text.lower()
            if any(cue in lowered for cue in _NOTE_CUES):
                out.append({"date": when.isoformat(), "ref": f"{DAILY_NOTES}/{path.relative_to(root)}:{number}", "text": text[:240]})
    return out


def ingest_readwise(interests: list[Interest], today: date, days: int, runner=None) -> list[str]:
    """Books with highlights in the window. The CLI is optional; absence is a
    note, not an error."""
    runner = runner or _run_readwise
    try:
        rows = runner(days)
    except FileNotFoundError:
        return ["readwise: CLI not installed"]
    except Exception as exc:  # network, auth, shape
        return [f"readwise: unavailable: {exc!r}"]
    since = today - timedelta(days=days)
    added = 0
    for row in rows:
        title = str(row.get("book_title") or "").strip()
        category = str(row.get("book_category") or "").lower()
        stamp = str(row.get("highlighted_at") or row.get("updated") or "")[:10]
        if not title or category not in ("books", "book") or not stamp:
            continue
        try:
            when = date.fromisoformat(stamp)
        except ValueError:
            continue
        if when < since:
            continue
        _, ok = upsert_event(interests, title, "book", Event(stamp, "read", "readwise", f"readwise:{row.get('book_id', '')}"))
        added += int(ok)
    return [f"readwise: {added} new book events"]


def _run_readwise(days: int) -> list[dict[str, Any]]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    proc = subprocess.run(
        [
            "readwise",
            "--json",
            "readwise-list-highlights",
            "--page-size",
            "100",
            "--highlighted-at-gt",
            since,
            "--response-fields",
            "book_id,book_title,book_category,highlighted_at,updated",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    data = json.loads(proc.stdout or "{}")
    return data.get("results", data) if isinstance(data, dict) else data


# ---------------------------------------------------------------- views


def summary_rows(interests: list[Interest], today: date, include_all: bool = False) -> list[dict[str, Any]]:
    rows = []
    for it in sorted(interests, key=lambda i: (-i.strength(today), i.name)):
        status = it.derived_status(today)
        if not include_all and status in ("dormant", "declined"):
            continue
        rows.append(
            {
                "slug": it.slug,
                "name": it.name,
                "kind": it.kind,
                "status": status,
                "strength": it.strength(today),
                "events": len(it.events),
                "last_event": it.last_event(),
                "declared": it.declared,
            }
        )
    return rows


def text_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(empty ledger)"
    width = max(len(r["name"]) for r in rows)
    out = []
    for r in rows:
        out.append(
            f"{r['status']:8} {r['strength']:5.2f}  {r['kind']:10} {r['name']:<{width}}  "
            f"{r['events']} ev  last {r['last_event'] or '-'}"
        )
    return "\n".join(out)


# ---------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ov", help="Vault root (default: $OV via _paths).")
    parser.add_argument("--today", help="Override today's date (YYYY-MM-DD) for tests.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Ledger with strength and status.")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--all", action="store_true", help="Include dormant and declined.")

    p_active = sub.add_parser("active", help="Interests routines should search.")
    p_active.add_argument("--json", action="store_true")
    p_active.add_argument("--kind", help="Only this kind.")

    p_add = sub.add_parser("add", help="Record an event, creating the interest if new.")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--kind", default="other", choices=KINDS)
    p_add.add_argument("--event", default="declared", choices=sorted(EVENT_WEIGHTS))
    p_add.add_argument("--date", help="YYYY-MM-DD (default today).")
    p_add.add_argument("--source", default="manual")
    p_add.add_argument("--ref", default="")
    p_add.add_argument("--declared", action="store_true", help="Mark as user-declared (never drops below watch).")

    for verb in ("decline", "undecline"):
        p = sub.add_parser(verb)
        p.add_argument("slug")

    p_ev = sub.add_parser("evidence", help="What needs a reading: pending rows and recent diary candidates.")
    p_ev.add_argument("--days", type=int, default=10)
    p_ev.add_argument("--json", action="store_true")
    p_res = sub.add_parser("resolve", help="Clear a pending row after it has been judged.")
    p_res.add_argument("pending_id")

    p_ingest = sub.add_parser("ingest", help="Pull events from the structured sources.")
    p_ingest.add_argument("--source", default="all", choices=["all", "anilist", "experience-log", "readwise"])
    p_ingest.add_argument("--days", type=int, default=45)
    p_ingest.add_argument("--dry-run", action="store_true")

    sub.add_parser("recompute", help="Rewrite statuses from strength.")

    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else date.today()
    try:
        ov = Path(args.ov).expanduser() if args.ov else vault_root()
    except PathsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ledger = ov / LEDGER
    interests = load_ledger(ledger)
    pending = load_pending(ledger)

    def save() -> None:
        recompute(interests, today)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(ledger, dump_ledger(interests, pending))

    if args.cmd == "list":
        rows = summary_rows(interests, today, include_all=args.all)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else text_table(rows))
        return 0

    if args.cmd == "active":
        rows = [r for r in summary_rows(interests, today) if r["status"] in ("active", "watch")]
        if args.kind:
            rows = [r for r in rows if r["kind"] == args.kind]
        declined = sorted(it.name for it in interests if it.status == "declined")
        if args.json:
            print(json.dumps({"today": today.isoformat(), "interests": rows, "declined": declined}, ensure_ascii=False, indent=2))
        else:
            print(text_table(rows))
            if declined:
                print("declined: " + ", ".join(declined))
        return 0

    if args.cmd == "add":
        when = args.date or today.isoformat()
        date.fromisoformat(when)
        it, ok = upsert_event(
            interests, args.name, args.kind, Event(when, args.event, args.source, args.ref), declared=args.declared, dedupe=False
        )
        save()
        print(f"{'recorded' if ok else 'unchanged'} {it.name} ({it.kind}) {args.event} {when}; status {it.status}, strength {it.strength(today)}")
        return 0

    if args.cmd in ("decline", "undecline"):
        it = next((i for i in interests if i.slug == args.slug), None) or find(interests, args.slug)
        if it is None:
            print(f"error: no interest {args.slug!r}", file=sys.stderr)
            return 1
        it.status = "declined" if args.cmd == "decline" else "watch"
        save()
        print(f"{it.name}: {it.status}")
        return 0

    if args.cmd == "evidence":
        rows = sorted(pending, key=lambda x: x.date)
        candidates = note_candidates(ov, today, args.days)
        if args.json:
            print(json.dumps({
                "today": today.isoformat(),
                "pending": [vars(pd) for pd in rows],
                "diary_candidates": candidates,
                "known": [it.name for it in interests],
            }, ensure_ascii=False, indent=2))
            return 0
        print("pending (attended, not yet judged):" if rows else "pending: none")
        for pd in rows:
            print(f"  {pd.id}  {pd.date}  {pd.category}  {pd.title}")
        print(f"diary candidates, last {args.days} days:" if candidates else f"diary candidates, last {args.days} days: none")
        for c in candidates:
            print(f"  {c['date']}  {c['ref']}  {c['text']}")
        return 0

    if args.cmd == "resolve":
        pd = next((x for x in pending if x.id == args.pending_id), None)
        if pd is None:
            print(f"error: no pending row {args.pending_id!r}", file=sys.stderr)
            return 1
        pending.remove(pd)
        save()
        print(f"resolved {pd.title}")
        return 0

    if args.cmd == "ingest":
        notes: list[str] = []
        state = _load_state(ov)
        if args.source in ("all", "anilist"):
            notes += ingest_anilist(ov, interests, today, state)
        if args.source in ("all", "experience-log"):
            notes += ingest_experience_log(ov, interests, today, pending)
        if args.source in ("all", "readwise"):
            notes += ingest_readwise(interests, today, args.days)
        for note in notes:
            print(note)
        if args.dry_run:
            print("(dry run; ledger and state not written)")
        else:
            save()
            _save_state(ov, state)
            print(f"wrote {fmt(ledger)} ({len(interests)} interests, {len(pending)} pending)")
        return 0

    if args.cmd == "recompute":
        save()
        print(text_table(summary_rows(interests, today, include_all=True)))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
