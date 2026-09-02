#!/usr/bin/env python3
"""daily_brief.py: Assemble the action surface that goes above the fold.

Why this exists: the daily digest's first screen is the only part read before the
day's deep work, so it is the scarcest real estate in the system. Everything
that competes for it already has an engine -- `deadlines.py` (expiring perks,
open windows), `recurring.py` (obligations), `todos.py` (dated TODOs),
`cues.py` (review debt), and optionally a private feature's derived cache
(episode and ticket reminders). None of them decides what gets cut. That
decision is what this script owns.

The triage rule, in priority order:

  1. Forfeitable, inside its own lead time -> itemized. Missing it destroys value,
     and each row declares how early it needs the attention.
  1b. Milestone, inside its own lead time   -> itemized under 本季主线. Nothing is
     forfeited, but this is the quarter's main line, and a screen that only
     ever pulls toward "don't lose money" never pulls toward it. Two or three
     rows, never folded, so the first screen always names what the quarter is for.
  2. Dated and due within a week           -> itemized. Slips gracefully, still needs a slot.
  3. Everything else                       -> folded into a count line.
  3b. Health observability                 -> one count line, always: days since
     the newest weight row. A lapsed measurement is the failure nothing else fires on.

The rule that matters most is #3, and specifically this: **overdue is not
urgent.** A rotation task hundreds of days overdue is not an emergency, it is a
mis-specified task. Listing nine overdue recurring items individually would
train the reader to skip the whole screen, so overdue items fold to a count and
only the ones that came due around now get their own bullet.

A hard line cap enforces the same discipline structurally: when the assembled
screen exceeds it, groups fold from the bottom up rather than the screen growing.

Every input degrades independently. A missing deadline index, an unreadable
cache, or a raising cue check produces a warning line and an otherwise complete
brief; it never blanks the screen. Stale inputs are reported as stale rather
than presented as current, because a confidently wrong date above the fold is
worse than an admitted gap.

Usage:
    uv run scripts/daily_brief.py                     # terminal view
    uv run scripts/daily_brief.py --json              # for routine_digest render
    uv run scripts/daily_brief.py --cap 8 --today 2099-01-31
    uv run scripts/daily_brief.py --skip-cues         # fast path, no vault walk

Exit codes: 0 even when every input is missing. The brief's job is to report the
state of the day, and "nothing is known" is a reportable state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deadlines as dl  # noqa: E402
from _paths import PathsError, tier, vault_root  # noqa: E402

BRIEF_SCHEMA = 1

DEFAULT_CAP = 12
TODO_HORIZON_DAYS = 7
FRESH_RECURRING_DAYS = 2
ITEM_TEXT_CHARS = 96
# Reminder feeds arrive pre-rendered as one sentence whose action-relevant tail
# ("N days out, not bought yet") sits at the end. Truncating at the item width
# would keep the flavour and drop the decision, so these get their own budget.
REMINDER_TEXT_CHARS = 200

# Optional reminder feed. The path is declared in private vault config, not
# here, so this script stays vault-agnostic and carries no private feature's
# layout:
#
#     # $OV/_meta/brief_sources.toml
#     [tracking]
#     cache = "<tier-relative path under $OV>"
#
# Absent config means no reminder lines, which is the correct default rather
# than a warning: not every vault has such a feature.
SOURCES_CONFIG = "_meta/brief_sources.toml"

# Health observability. `directions.md` marks restoring it as immediate, and a
# lapsed measurement is the failure that hides best: nothing fires when a table
# simply stops getting rows. One count line, always present, from the newest
# dated row of this section of `<paths.health>/metrics.md`.
HEALTH_METRICS_FILE = "metrics.md"
HEALTH_SECTION = "Body composition"

# Review-debt cues worth one folded line. `recurring` is excluded because this
# brief has its own recurring group and would otherwise double-count it.
REVIEW_CUES = (
    "weekly",
    "meta_reflection",
    "autoevo_pending",
    "aggregate_freshness",
    "routine_outputs",
    "career_growth",
)


@dataclass
class Item:
    text: str
    due: str | None = None
    days_left: int | None = None
    source: str | None = None


@dataclass
class Group:
    tier: int
    kind: str
    heading: str
    items: list[Item] = field(default_factory=list)
    folded: bool = False
    fold_heading: str = ""

    def rendered_lines(self) -> int:
        return 1 + (0 if self.folded else len(self.items))

    def display_heading(self) -> str:
        return self.fold_heading if self.folded and self.fold_heading else self.heading


# ---------------------------------------------------------------- loaders


def _truncate(text: str, limit: int = ITEM_TEXT_CHARS) -> str:
    """Shorten to one glanceable line, never mid-word.

    A cut inside a latin word ("do not bui…") costs more than the characters it
    saves: the reader stops to reconstruct it, which is the opposite of what a
    line above the fold is for. CJK has no such boundary and cuts anywhere, so
    the retreat only applies when it does not throw away most of the budget.
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ·,;:-") + "…"


def clean_todo_text(text: str) -> str:
    """Strip inline metadata and markdown so a TODO reads as one plain line.

    `todos.py` keeps the raw checkbox content in `Todo.text`, tokens included,
    because its own list view prints them as tags. Above the fold they are
    noise, and a link truncated mid-`](` is worse than noise. The token
    vocabulary is imported from `todos.INLINE_META` rather than restated, so a
    new token there needs no edit here.
    """
    try:
        import todos

        for regex in todos.INLINE_META.values():
            text = regex.sub("", text)
    except ImportError:  # pragma: no cover - import guard
        pass
    try:
        import routine_digest

        text = routine_digest.strip_inline_markup(text)
    except ImportError:  # pragma: no cover - import guard
        pass
    text = " ".join(text.split())
    return text.rstrip(" —-·,;:")


def _days_phrase(days_left: int) -> str:
    if days_left < 0:
        return f"逾期 {-days_left}d"
    if days_left == 0:
        return "今天"
    if days_left == 1:
        return "明天"
    return f"{days_left}d"


def load_closing(ov: Path, today: date, warnings: list[str]) -> list[Group]:
    """Forfeitable obligations, split into closing now versus needing a start."""
    index = dl.load_index(ov, today)
    warning = index.warning()
    if warning:
        warnings.append(warning)
    if index.errors:
        warnings.append(f"deadline index has {len(index.errors)} schema errors (deadlines.py lint)")

    # Each row declares how early it needs attention, because a uniform horizon
    # is wrong for this data: a card credit is actionable in a week, an award
    # night that needs a hotel booked is not.
    rows = [d for d in dl.in_lead_window(index) if d.is_forfeitable()]
    now_rows = [d for d in rows if d.days_left <= 1]
    lead_rows = [d for d in rows if d.days_left > 1]

    groups: list[Group] = []
    if now_rows:
        groups.append(
            Group(
                tier=1,
                kind="closing_now",
                heading=f"今天/明天关窗 {len(now_rows)} 件",
                items=[_closing_item(d) for d in now_rows],
                fold_heading=f"今天/明天关窗 {len(now_rows)} 件 (deadlines.py due --lead)",
            )
        )
    if lead_rows:
        groups.append(
            Group(
                tier=1,
                kind="closing_lead",
                heading=f"需要开始处理 {len(lead_rows)} 件",
                items=[_closing_item(d) for d in lead_rows],
                fold_heading=f"需要开始处理 {len(lead_rows)} 件 (deadlines.py due --lead)",
            )
        )
    return groups


def load_focus(ov: Path, today: date, warnings: list[str]) -> list[Group]:
    """The quarter's main line: milestone rows inside their lead time.

    Reads the same index as `load_closing`, which already reported its
    warnings, so this loader stays silent about index state. Tier 1 because
    it must survive the cap: folding the quarter's purpose into a count line
    is exactly the failure this slot exists to prevent.
    """
    del warnings  # reported by load_closing on the same index
    index = dl.load_index(ov, today)
    rows = [d for d in dl.in_lead_window(index) if d.kind == "milestone"]
    if not rows:
        return []
    return [
        Group(
            tier=1,
            kind="focus",
            heading=f"本季主线 {len(rows)} 件",
            items=[_closing_item(d) for d in rows],
            fold_heading=f"本季主线 {len(rows)} 件 (deadlines.py list --kind milestone)",
        )
    ]


def _closing_item(row: dl.Deadline) -> Item:
    text = f"{row.label} · {_days_phrase(row.days_left)}"
    if row.action:
        text += f" · {row.action}"
    return Item(text=_truncate(text), due=row.due, days_left=row.days_left, source=row.source)


def load_todos(_ov: Path, today: date, warnings: list[str]) -> list[Group]:
    """Dated TODOs inside the horizon. Undated TODOs never reach this screen."""
    try:
        import todos
    except ImportError as exc:  # pragma: no cover - import guard
        warnings.append(f"todos.py unavailable: {exc!r}")
        return []
    try:
        open_todos = todos.collect_open_todos(load_age=False)
    except Exception as exc:
        warnings.append(f"todo scan failed: {exc!r}")
        return []

    dated: list[tuple[int, Any]] = []
    for todo in open_todos:
        if not todo.due:
            continue
        try:
            due = date.fromisoformat(todo.due)
        except ValueError:
            continue
        days_left = (due - today).days
        if days_left <= TODO_HORIZON_DAYS:
            dated.append((days_left, todo))
    if not dated:
        return []

    dated.sort(key=lambda pair: (pair[0], pair[1].text))
    items = [
        Item(
            text=_truncate(f"{_days_phrase(days)} · {clean_todo_text(todo.text)}"),
            due=todo.due,
            days_left=days,
            source=f"{todo.short_source()}:{todo.line}",
        )
        for days, todo in dated
    ]
    return [
        Group(
            tier=2,
            kind="todo",
            heading=f"TODO 到期 {len(items)} 件",
            items=items,
            fold_heading=f"TODO 到期 {len(items)} 件 (todos.py list)",
        )
    ]


def load_recurring(_ov: Path, today: date, warnings: list[str]) -> list[Group]:
    """Recurring obligations, folded by design.

    Only items that came due in the last `FRESH_RECURRING_DAYS` days get a
    bullet. A long-overdue item is a specification problem, not today's news,
    and itemizing nine of them is how a morning screen becomes unreadable.
    """
    try:
        import recurring
    except ImportError as exc:  # pragma: no cover - import guard
        warnings.append(f"recurring.py unavailable: {exc!r}")
        return []
    try:
        rows = recurring.parse_file()
    except Exception as exc:
        warnings.append(f"recurring scan failed: {exc!r}")
        return []

    overdue = [r for r in rows if r.status(today) == "overdue"]
    due_soon = [r for r in rows if r.status(today) == "due-soon"]
    if not overdue and not due_soon:
        return []

    # "Just came due" spans both sides of the boundary: an item due today or
    # tomorrow and one that went overdue yesterday are the same actionable
    # thing. Only the long-overdue tail is noise.
    fresh = sorted(
        (
            r
            for r in overdue + due_soon
            if -FRESH_RECURRING_DAYS <= r.days_until_due(today) <= 1
        ),
        key=lambda r: r.days_until_due(today),
    )
    parts = []
    if overdue:
        parts.append(f"{len(overdue)} 条逾期")
    if due_soon:
        parts.append(f"{len(due_soon)} 条 ≤7d")
    if fresh:
        parts.append(f"{len(fresh)} 条刚到期")
    heading = "recurring: " + ", ".join(parts)

    items = [
        Item(
            text=_truncate(f"{_days_phrase(r.days_until_due(today))} · {r.slug} (every:{r.every_str()})"),
            due=r.next_due().isoformat(),
            days_left=r.days_until_due(today),
            source=f"gtd/recurring.md:{r.line}",
        )
        for r in fresh[:3]
    ]
    return [
        Group(
            tier=3,
            kind="recurring",
            heading=heading,
            items=items,
            fold_heading=f"{heading} (recurring.py list)",
        )
    ]


def load_review(ov: Path, today: date, warnings: list[str]) -> list[Group]:
    """Review debt, always one folded line.

    Deliberately does not restate each cue's wording: `cues.py` owns that text
    and duplicating it here would create a second source of truth. The brief
    contributes the count and the keys; the messages ride along as bullets that
    the cap is free to drop.
    """
    try:
        import cues
    except ImportError as exc:  # pragma: no cover - import guard
        warnings.append(f"cues.py unavailable: {exc!r}")
        return []

    checks = {name: fn for name, fn in cues.CHECKS}
    try:
        snoozes = cues._load_snoozes(ov)
    except Exception:
        snoozes = {}

    fired: list[tuple[str, str]] = []
    for key in REVIEW_CUES:
        fn = checks.get(key)
        if fn is None:
            continue
        try:
            cue, _ = fn(ov, today)
        except Exception as exc:
            warnings.append(f"cue {key} raised {exc!r}")
            continue
        if not cue:
            continue
        try:
            if cues._is_snoozed(snoozes, key, today):
                continue
        except Exception:
            pass
        fired.append((key, cue.message))
    if not fired:
        return []

    keys = " · ".join(key for key, _ in fired)
    heading = f"review 债 {len(fired)} 项: {keys}"
    return [
        Group(
            tier=3,
            kind="review",
            heading=heading,
            items=[Item(text=_truncate(message)) for _, message in fired],
            fold_heading=heading,
        )
    ]


def tracking_cache_path(ov: Path) -> Path | None:
    """Resolve the optional reminder cache from private vault config."""
    config = ov / SOURCES_CONFIG
    if not config.is_file():
        return None
    try:
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    relative = (raw.get("tracking") or {}).get("cache")
    if not isinstance(relative, str) or not relative.strip():
        return None
    candidate = (ov / relative.strip()).resolve()
    # Refuse a configured path that escapes the vault.
    try:
        candidate.relative_to(ov.resolve())
    except ValueError:
        return None
    return candidate


def _tracking_section_age(
    section: object,
    today: date,
    fallback_refreshed: str,
) -> int | None:
    """Age one independently refreshed cache section.

    Legacy caches only have the top-level ``refreshed_at``.  New producers
    stamp each section so a successful AniList request cannot make an old
    concert reminder look current, or vice versa.
    """
    if not isinstance(section, dict):
        return _age_days(fallback_refreshed[:10], today)
    raw = section.get("last_success_at") or fallback_refreshed or section.get("date")
    return _age_days(str(raw or "")[:10], today)


def _tracking_failure_warning(name: str, section: object) -> str | None:
    if not isinstance(section, dict) or not section.get("failed_at"):
        return None
    error = str(section.get("error") or "unknown error")
    return f"{name} refresh failed at {section['failed_at']}: {error}"


def latest_table_date(text: str, section: str) -> str | None:
    """Newest ISO date in the first cell of a markdown table under `section`.

    The cell is usually a link (`[2026-04-11](../daily-notes/...)`), so the
    date is searched for rather than parsed as the whole cell.
    """
    heading = re.compile(rf"^#{{1,6}}\s+{re.escape(section)}\s*$", re.MULTILINE)
    match = heading.search(text)
    if not match:
        return None
    rest = text[match.end():]
    nxt = re.search(r"^#{1,6}\s", rest, re.MULTILINE)
    block = rest[: nxt.start()] if nxt else rest
    dates: list[str] = []
    for line in block.splitlines():
        if not line.startswith("|"):
            continue
        first = line.strip("|").split("|", 1)[0]
        found = re.search(r"\d{4}-\d{2}-\d{2}", first)
        if found:
            dates.append(found.group(0))
    return max(dates) if dates else None


def load_health(_ov: Path, today: date, warnings: list[str]) -> list[Group]:
    """One line: how old the newest body-composition row is.

    Always rendered when the metrics file exists, because the number that
    matters is the one that has quietly grown large. A missing file is a
    warning rather than silence: this vault declared the tier.
    """
    try:
        path = tier("health") / HEALTH_METRICS_FILE
    except (PathsError, KeyError):
        return []
    if not path.is_file():
        warnings.append(f"health metrics missing ({HEALTH_METRICS_FILE}); observability unknown")
        return []
    try:
        latest = latest_table_date(path.read_text(encoding="utf-8"), HEALTH_SECTION)
    except OSError as exc:
        warnings.append(f"health metrics unreadable: {exc!r}")
        return []
    if latest is None:
        heading = f"健康观测: {HEALTH_SECTION} 无记录 ({HEALTH_METRICS_FILE})"
    else:
        age = _age_days(latest, today)
        heading = f"健康观测: 体重上次 {latest} ({age}d 前) ({HEALTH_METRICS_FILE})"
    # Born folded: it is a count line, so the cap may merge it with the others.
    return [Group(tier=3, kind="health", heading=heading, fold_heading=heading, folded=True)]


def load_tracking(ov: Path, today: date, warnings: list[str]) -> list[Group]:
    """Episode and ticket reminders from an optional derived cache.

    Read-only and offline: whichever feature owns the cache refreshes it on its
    own schedule. A stale cache is reported, never silently shown as today's.
    """
    cache_path = tracking_cache_path(ov)
    if cache_path is None or not cache_path.is_file():
        return []
    label = cache_path.name
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        warnings.append(f"reminder cache unreadable: {exc!r}")
        return []
    if not isinstance(data, dict):
        warnings.append("reminder cache is not an object")
        return []

    groups: list[Group] = []
    refreshed = str(data.get("refreshed_at") or "")
    anime = data.get("anime")
    followups = data.get("followups")
    concerts = data.get("concerts")
    anime = anime if isinstance(anime, dict) else {}
    followups = followups if isinstance(followups, dict) else {}
    concerts = concerts if isinstance(concerts, dict) else {}
    for name, section in (
        ("anime", anime),
        ("anime follow-up", followups),
        ("concert", concerts),
    ):
        warning = _tracking_failure_warning(name, section)
        if warning:
            warnings.append(warning)

    updates = [str(u) for u in (anime.get("updates") or []) if str(u).strip()]
    updates.extend(
        str(u) for u in (followups.get("updates") or []) if str(u).strip()
    )
    updates = list(dict.fromkeys(updates))
    visible_ages: list[int] = []
    if updates:
        anime_age = _tracking_section_age(anime, today, refreshed)
        followup_age = _tracking_section_age(followups, today, refreshed)
        used_ages = [value for value in (anime_age, followup_age) if value is not None]
        age = max(used_ages) if used_ages else None
        if age is not None:
            visible_ages.append(age)
        suffix = f" [缓存 {age}d 前]" if age and age > 1 else ""
        shown = " · ".join(_truncate(u, REMINDER_TEXT_CHARS) for u in updates[:3])
        extra = f" · +{len(updates) - 3}" if len(updates) > 3 else ""
        groups.append(
            Group(
                tier=3,
                kind="anime",
                heading=f"动漫更新: {shown}{extra}{suffix}",
                items=[],
                folded=True,
            )
        )

    reminders = concerts.get("reminders") or []
    formatted = [_format_reminder(r) for r in reminders]
    formatted = [f for f in formatted if f]
    if formatted:
        age = _tracking_section_age(concerts, today, refreshed)
        if age is not None:
            visible_ages.append(age)
        suffix = f" [缓存 {age}d 前]" if age and age > 1 else ""
        shown = " · ".join(formatted[:3])
        extra = f" · +{len(formatted) - 3}" if len(formatted) > 3 else ""
        groups.append(
            Group(
                tier=1,
                kind="concert",
                heading=f"演唱会: {shown}{extra}{suffix}",
                items=[],
                folded=True,
            )
        )

    if not refreshed and all(
        _tracking_section_age(section, today, "") is None
        for section in (anime, followups, concerts)
    ):
        warnings.append(f"reminder cache has no refreshed_at ({label})")
    stale_age = max(visible_ages) if visible_ages else None
    if stale_age is not None and stale_age > 1:
        warnings.append(
            f"reminder cache stale {stale_age}d ({label}); episode and ticket lines may be out of date"
        )
    return groups


_BRACKETED_RATIONALE = re.compile(r"\s*[\[［][^\]］]{20,}[\]］]")


def _format_reminder(raw: Any) -> str:
    """Tolerant formatter: the cache may hold pre-rendered strings or records.

    A long bracketed clause is always dropped. The reminder feed writes its
    reasoning inline ("[... why this act is worth testing ...]"), which belongs
    in the tracker and not on a line whose job is to be read at a glance. It is
    dropped unconditionally rather than only when over the cap, because a
    hundred-character line is already too long to glance at even though it fits;
    the decision at the end of the sentence is what has to survive, and a plain
    truncation would have cut exactly that. Short bracketed text survives: the
    twenty-character floor separates an aside from a label.
    """
    if isinstance(raw, str):
        raw = _BRACKETED_RATIONALE.sub("", raw, count=1)
        return _truncate(raw, REMINDER_TEXT_CHARS)
    if not isinstance(raw, dict):
        return ""
    bits = [str(raw[k]) for k in ("artist", "sale_date", "date", "city", "venue") if raw.get(k)]
    return _truncate(" ".join(bits), REMINDER_TEXT_CHARS) if bits else ""


def _age_days(iso_date: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(iso_date)).days
    except ValueError:
        return None


# ---------------------------------------------------------------- assembly


MERGED_HEADING_CHARS = 220


def apply_cap(groups: list[Group], cap: int) -> tuple[list[Group], int, bool]:
    """Fold groups from the bottom up until the screen fits.

    Tier 1 is never folded. Those items are forfeitable and inside a week, which
    is the entire reason this screen exists; hiding them to satisfy a line count
    would defeat the cap's own purpose. If tier 1 alone overflows, the brief
    reports `over_cap` and overflows honestly -- many things closing at once is
    a real signal, not something to compress away.

    Order within the foldable tiers is descending tier then descending position,
    so the browsable counts collapse before the dated ones. When folding alone
    is not enough, the tier-3 count lines merge into a single line: the counts
    survive, the line budget holds.

    Returns the groups, how many the cap folded, and whether it still overflows.
    """
    folded_by_cap = 0

    def total() -> int:
        return sum(g.rendered_lines() for g in groups)

    for level in (3, 2):
        indices = sorted((i for i, g in enumerate(groups) if g.tier == level), reverse=True)
        for index in indices:
            if total() <= cap:
                break
            group = groups[index]
            if group.folded or not group.items:
                continue
            group.folded = True
            folded_by_cap += 1

    if total() > cap:
        # `g.folded` is the test, not `not g.items`: a group the cap just folded
        # still holds its items list, it simply does not render them.
        mergeable = [g for g in groups if g.tier == 3 and g.folded]
        if len(mergeable) > 1:
            merged_ids = {id(g) for g in mergeable}
            heading = " · ".join(g.display_heading() for g in mergeable)
            if len(heading) > MERGED_HEADING_CHARS:
                heading = heading[: MERGED_HEADING_CHARS - 1] + "…"
            groups = [g for g in groups if id(g) not in merged_ids]
            groups.append(Group(tier=3, kind="merged", heading=heading, folded=True))
            groups.sort(key=lambda g: g.tier)

    return groups, folded_by_cap, total() > cap


def build(
    ov: Path,
    today: date,
    *,
    cap: int = DEFAULT_CAP,
    skip_cues: bool = False,
) -> dict[str, Any]:
    warnings: list[str] = []
    groups: list[Group] = []
    groups.extend(load_closing(ov, today, warnings))
    groups.extend(load_focus(ov, today, warnings))
    groups.extend(load_todos(ov, today, warnings))
    groups.extend(load_recurring(ov, today, warnings))
    if not skip_cues:
        groups.extend(load_review(ov, today, warnings))
    groups.extend(load_health(ov, today, warnings))
    groups.extend(load_tracking(ov, today, warnings))

    groups.sort(key=lambda g: g.tier)
    groups, folded_by_cap, over_cap = apply_cap(groups, cap)
    if over_cap:
        warnings.append(
            f"{sum(g.rendered_lines() for g in groups)} lines over the {cap}-line cap; "
            "forfeitable items are never folded"
        )

    return {
        "schema": BRIEF_SCHEMA,
        "date": today.isoformat(),
        "cap": cap,
        "rendered_lines": sum(g.rendered_lines() for g in groups),
        "folded_by_cap": folded_by_cap,
        "over_cap": over_cap,
        "groups": [
            {
                "tier": g.tier,
                "kind": g.kind,
                "heading": g.display_heading(),
                "folded": g.folded,
                "items": [
                    {
                        k: v
                        for k, v in (
                            ("text", i.text),
                            ("due", i.due),
                            ("days_left", i.days_left),
                            ("source", i.source),
                        )
                        if v is not None
                    }
                    for i in (g.items if not g.folded else [])
                ],
            }
            for g in groups
        ],
        "warnings": warnings,
    }


def text_view(brief: dict[str, Any]) -> str:
    lines = [f"# 今日 {brief['date']}  ({brief['rendered_lines']}/{brief['cap']} 行)"]
    if not brief["groups"]:
        lines.append("  今天没有关窗项、到期 TODO 或 review 债。")
    for group in brief["groups"]:
        lines.append(group["heading"])
        for item in group["items"]:
            lines.append(f"  · {item['text']}")
    for warning in brief["warnings"]:
        lines.append(f"! {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble the daily action surface (the digest's first screen).",
    )
    parser.add_argument("--json", action="store_true", help="JSON for routine_digest render.")
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP, help="Hard rendered-line cap.")
    parser.add_argument("--today", help="Override today (YYYY-MM-DD) for testing.")
    parser.add_argument(
        "--skip-cues", action="store_true", help="Skip review-debt checks (avoids a vault walk)."
    )
    parser.add_argument("--out", help="Write to a file instead of stdout.")
    args = parser.parse_args(argv)

    try:
        ov = vault_root()
    except PathsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.today:
        try:
            today = date.fromisoformat(args.today)
        except ValueError:
            print(f"--today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
            return 1
    else:
        today = effective_today()

    if args.cap < 1:
        print("--cap must be >= 1", file=sys.stderr)
        return 1

    brief = build(ov, today, cap=args.cap, skip_cues=args.skip_cues)
    payload = json.dumps(brief, indent=2, ensure_ascii=False) if args.json else text_view(brief)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.out} ({brief['rendered_lines']} lines)")
    else:
        print(payload)
    return 0


def effective_today(now: datetime | None = None) -> date:
    """Today, or yesterday before 03:00 -- the harness day boundary."""
    now = now or datetime.now()
    return now.date() - timedelta(days=1) if now.hour < 3 else now.date()


if __name__ == "__main__":
    sys.exit(main())
