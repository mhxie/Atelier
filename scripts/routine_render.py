"""routine_render.py: render the digest manifest into mail-safe HTML.

Split out of routine_digest.py; routine_digest.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import sys

import html as html_mod
import re
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PathsError, vault_root  # noqa: E402
from routine_collect import strip_frontmatter  # noqa: E402
from routine_digest_core import (  # noqa: E402
    DEFAULT_ROUTINE_LINES,
    _within_days,
    deep_read_lane_gap,
    humanize_slug,
    iter_sources,
)


_INLINE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")

_INLINE_CODE = re.compile(r"`([^`]+)`")

_INLINE_BOLD = re.compile(r"\*\*([^*]+)\*\*")

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

# Source-index render budget. The index is navigation, not content.
_INDEX_ITEM_CAP = 5

_INDEX_EXCERPT_CAP = 180

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
        strip = _render_signals(manifest["health"], brief, overview)
        if strip:
            parts.append(strip)
        meta_bits.extend(_fleet_bits(manifest["health"], brief))
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

    # Inputs that failed or were skipped this run. They belong here, with the
    # provenance, and not in a section above the fold: a missing Readwise
    # pull changes nothing the reader does in the next twelve hours.
    gaps = [str(g).strip() for g in (overview.get("gaps") or []) if str(g).strip()]
    if gaps:
        rendered = "<br>".join(html_mod.escape(g) for g in gaps)
        depth.append(f'<p style="{_S_NOTE}">输入缺口<br>{rendered}</p>')

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
    """Counts the colophon borrows from the brief: recurring overdue and
    review debt. Parsed from the group headings the brief already prints, so
    the colophon and the ledger can never disagree."""
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

# Days since the last weight row past which the masthead paints it red. This is
# the tracking cadence the digest assumes, not a fixed constant: the first
# morning past it is where "restore" has quietly become "lapsed".
WEIGHT_STALE_DAYS = 3

DECISION_SECTION = "需要的决策"

def _decision_count(overview: dict[str, Any] | None) -> int:
    for section in (overview or {}).get("sections") or []:
        if isinstance(section, dict) and section.get("title") == DECISION_SECTION:
            return len(section.get("bullets") or [])
    return 0

def _render_signals(
    health: dict[str, Any],
    brief: dict[str, Any] | None = None,
    overview: dict[str, Any] | None = None,
) -> str:
    """At most five numbers, each shown only when it changes the day.

    The strip used to carry fleet bookkeeping: files reported, cycles
    completed, review debt. Those are the same every morning, so the eye
    learned to skip the row, and the one number that mattered (failures)
    skipped with it. Now a cell appears only when its fact is live:

      关窗   forfeitable rows inside their lead time; red when one closes today
      主线   days to the nearest milestone on the quarter's main line
      体重   days since the last weight row; red past WEIGHT_STALE_DAYS
      决策   bullets under 需要的决策, the calls the routines could not make
      失败   failed routine cycles, only when there are any

    Bookkeeping moved to the colophon, where it is still on record. An empty
    strip renders nothing rather than a row of zeros.
    """
    signals = (brief or {}).get("signals") or {}
    cells: list[tuple[str, str, str]] = []
    closing = int(signals.get("closing", 0) or 0)
    if closing:
        hot = int(signals.get("closing_now", 0) or 0) > 0
        cells.append((str(closing), "关窗", _URGENT if hot else _ACCENT))
    if signals.get("focus_days") is not None:
        cells.append((f'{int(signals["focus_days"])}d', "主线", _ACCENT))
    if signals.get("weight_age_days") is not None:
        age = int(signals["weight_age_days"])
        cells.append((f"{age}d", "体重", _URGENT if age > WEIGHT_STALE_DAYS else _INK))
    decisions = _decision_count(overview)
    if decisions:
        cells.append((str(decisions), "决策", _ACCENT))
    failed = int(health.get("failed", 0) or 0)
    if failed:
        cells.append((str(failed), "失败", _URGENT))
    if not cells:
        return ""
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

def _fleet_bits(health: dict[str, Any], brief: dict[str, Any] | None) -> list[str]:
    """Fleet bookkeeping for the colophon: on record, off the masthead."""
    bits = [
        f'routine {health.get("reported", 0)}/{health.get("declared", 0)} 有产出',
        f'{health.get("completed", 0)} 完成',
        f'{health.get("failed", 0)} 失败',
        f'{health.get("review_debt", 0)} 待 review',
    ]
    extra = _brief_counts(brief)
    if "recurring_overdue" in extra:
        bits.append(f'recurring 逾期 {extra["recurring_overdue"]}')
    if "review_debt" in extra:
        bits.append(f'review 债 {extra["review_debt"]}')
    return bits

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

    A leading frontmatter block is dropped first: it is machine bookkeeping
    (`date:`, `type:`, channel counts) that read as stray paragraphs when the
    raw-body fallback showed a routine file in full.

    Returns (html, truncated).
    """
    body = _MD_IMAGE.sub("", strip_frontmatter(text))
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
    if source.get("carried"):
        # Dated before this window and delivered by no earlier daily digest:
        # the routine finished after that morning's run.
        head += f' <span style="{_S_TRACE}">补录</span>'
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
