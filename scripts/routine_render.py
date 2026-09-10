"""Render a digest manifest through a local, locked MJML compiler."""

from __future__ import annotations

import html as html_mod
import json
import re
import sys
from functools import cache
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


_ROOT = Path(__file__).resolve().parents[1]
_MJML_BRIDGE = Path(__file__).resolve().with_name("mjml_render.mjs")
MJML_TIMEOUT_SECONDS = 20
MJML_INPUT_BYTES = 2_000_000

_FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', "
    "'Hiragino Sans GB', 'Noto Sans SC', Roboto, Helvetica, Arial, sans-serif"
)
_MONO = "Menlo, Consolas, 'SF Mono', ui-monospace, monospace"
_SERIF = "Georgia, 'Songti SC', 'Noto Serif SC', serif"
_INK = "#17201c"
_MUTED = "#5c6862"
_FAINT = "#8b968f"
_RULE = "#d9dfd9"
_ACCENT = "#0f6b5c"
_LINK = _ACCENT
_CHIP = "#eef1ec"
_URGENT = "#b23d27"
_AMBER = "#b8860b"
_OK = "#2f7d4f"
_BAR_TRACK = "#e3e8e3"
_S_STAT_TABLE = "digest-signal-strip"

_CSS = f"""
body {{ margin:0; padding:0; background:#ffffff; color:{_INK}; }}
h1.digest-title {{ margin:0; font-family:{_SERIF}; font-size:26px; font-weight:600; line-height:1.15; letter-spacing:-.01em; }}
.digest-date {{ color:{_ACCENT}; }}
.weather {{ font-size:13px; color:{_INK}; text-align:right; }}
.weather-sub {{ display:block; font-family:{_MONO}; font-size:11px; color:{_FAINT}; }}
.eyebrow {{ margin:0 0 8px; font-size:12px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:{_ACCENT}; }}
.eyebrow-meta, .section-count {{ margin-left:8px; font-family:{_MONO}; font-size:11px; font-weight:500; color:{_FAINT}; letter-spacing:0; text-transform:none; }}
h2.section-title {{ margin:0 0 9px; font-size:19px; font-weight:650; line-height:1.3; color:{_INK}; }}
h3.index-title {{ margin:14px 0 5px; font-size:14px; font-weight:600; color:{_MUTED}; }}
.lead {{ margin:0; font-family:{_SERIF}; font-size:18px; line-height:1.55; color:{_INK}; }}
.small {{ margin:0; font-size:12px; line-height:1.55; color:{_MUTED}; }}
.warning {{ color:{_URGENT}; }}
.note {{ margin:0; padding:9px 11px; border-left:3px solid {_RULE}; background:#f7f8f6; color:{_MUTED}; font-size:12px; line-height:1.55; }}
.colophon {{ margin:0; font-family:{_MONO}; font-size:11px; color:{_FAINT}; line-height:1.6; }}
.signal-number {{ display:block; font-family:{_SERIF}; font-size:24px; line-height:1; }}
.signal-label {{ display:block; margin-top:4px; font-size:11px; color:{_FAINT}; }}
table.digest-ledger {{ width:100%; border-collapse:collapse; border-top:1px solid {_RULE}; }}
.ledger-group {{ padding:12px 0 2px; font-size:12px; font-weight:600; color:{_MUTED}; }}
.group-hot {{ font-size:14px; font-weight:650; color:{_INK}; }}
.group-cool {{ font-size:13px; font-weight:600; color:{_MUTED}; }}
.ledger-due {{ width:56px; padding:10px 0; border-bottom:1px solid {_RULE}; vertical-align:top; white-space:nowrap; font-family:{_MONO}; font-size:12px; }}
.ledger-item {{ padding:10px 0 10px 12px; border-bottom:1px solid {_RULE}; vertical-align:top; line-height:1.55; }}
.hint {{ display:block; font-size:13px; color:{_MUTED}; }}
.trace {{ font-family:{_MONO}; font-size:11px; color:{_FAINT}; background:{_CHIP}; padding:1px 6px; border-radius:3px; white-space:nowrap; }}
.flag {{ font-family:{_MONO}; font-size:11px; color:{_URGENT}; background:#f6e8e4; padding:1px 6px; border-radius:3px; white-space:nowrap; }}
ul.digest-list, ul.source-index {{ margin:4px 0 0; padding-left:19px; }}
ul.source-index {{ font-size:12px; color:{_MUTED}; }}
ul.digest-list li, ul.source-index li {{ margin:0 0 7px; }}
li.decision {{ margin:0 0 10px; padding:10px 12px; border-left:3px solid {_ACCENT}; background:{_CHIP}; list-style:none; }}
.decision-question {{ margin:0 0 6px; font-weight:600; color:{_INK}; }}
.decision-options {{ margin:0 0 6px; }}
.decision-option {{ display:inline-block; min-width:16px; margin-right:6px; font-family:{_MONO}; color:{_ACCENT}; }}
.decision-settle {{ margin:0; font-size:12px; color:{_MUTED}; }}
.article {{ margin:0; padding:10px 0; border-top:1px solid {_RULE}; }}
.article-title, .deep-title {{ margin:0; font-weight:600; color:{_INK}; }}
.article-meta, .deep-meta {{ display:block; margin-top:2px; font-family:{_MONO}; font-size:11px; color:{_FAINT}; }}
.article-why {{ margin:5px 0 0; color:{_ACCENT}; }}
.article-abstract, .deep-body {{ margin:5px 0 0; color:{_MUTED}; }}
.lab-name {{ margin:12px 0 4px; font-weight:650; }}
.lab-category {{ font-family:{_MONO}; font-size:11px; color:{_ACCENT}; }}
.deep-item {{ margin:0; padding:12px 0; border-top:1px solid {_RULE}; }}
.deep-why {{ margin:6px 0 0; padding-left:12px; border-left:2px solid {_ACCENT}; color:{_MUTED}; }}
.feed-note {{ color:{_MUTED}; }}
.fold {{ margin:0; padding:8px 0; border-top:1px solid {_RULE}; border-bottom:1px solid {_RULE}; text-align:center; font-family:{_MONO}; font-size:11px; color:{_FAINT}; }}
.cost {{ margin-left:8px; font-family:{_MONO}; font-size:11px; font-weight:500; color:{_FAINT}; }}
.markdown p {{ margin:0 0 8px; line-height:1.65; }}
.markdown ul, .markdown ol {{ margin:0 0 8px; padding-left:19px; }}
.markdown li {{ margin:0 0 5px; }}
.markdown h1, .markdown h2, .markdown h3, .markdown h4, .markdown h5, .markdown h6 {{ margin:12px 0 6px; font-size:16px; font-weight:600; }}
.markdown table {{ width:100%; border-collapse:collapse; border-top:1px solid {_RULE}; }}
.markdown th, .markdown td {{ padding:6px; text-align:left; border-bottom:1px solid {_RULE}; }}
a {{ color:{_LINK}; text-decoration:underline; }}
code {{ font-family:{_MONO}; font-size:11px; color:{_MUTED}; background:{_CHIP}; padding:1px 4px; border-radius:3px; }}
""".strip()

MJNode = dict[str, Any]


def _node(
    tag: str,
    *,
    attrs: dict[str, Any] | None = None,
    content: str | None = None,
    children: list[MJNode] | None = None,
) -> MJNode:
    node: MJNode = {"tagName": tag, "attributes": attrs or {}}
    if content is not None:
        node["content"] = content
    else:
        node["children"] = children or []
    return node


def _text(content: str, *, css_class: str = "", **attrs: Any) -> MJNode:
    values: dict[str, Any] = {"padding": "0", **attrs}
    if css_class:
        values["css-class"] = css_class
    return _node("mj-text", attrs=values, content=content)


def _column(children: list[MJNode], *, width: str = "", valign: str = "top") -> MJNode:
    attrs = {"padding": "0", "vertical-align": valign}
    if width:
        attrs["width"] = width
    return _node("mj-column", attrs=attrs, children=children)


def _section(
    children: list[MJNode],
    *,
    padding: str = "12px 20px 0",
    css_class: str = "",
    **attrs: Any,
) -> MJNode:
    values: dict[str, Any] = {"padding": padding, **attrs}
    if css_class:
        values["css-class"] = css_class
    return _node("mj-section", attrs=values, children=[_column(children)])


def _heading(title: str, badge: str = "") -> MJNode:
    return _text(
        f'<h2 class="section-title">{html_mod.escape(title)}{badge}</h2>'
    )


def _node_text(nodes: list[MJNode]) -> str:
    parts: list[str] = []

    def visit(node: MJNode) -> None:
        if "content" in node:
            parts.append(str(node["content"]))
        for child in node.get("children") or []:
            visit(child)

    for node in nodes:
        visit(node)
    return "\n".join(parts)


def _compile_mjml(document: MJNode) -> str:
    """Compile through the checked-in bridge and a system-resolved Node."""
    import _node

    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MJML_INPUT_BYTES:
        raise RuntimeError(f"MJML input exceeds {MJML_INPUT_BYTES} bytes")
    try:
        completed = _node.run(
            [_MJML_BRIDGE],
            input=payload,
            cwd=_ROOT,
            env=_node.system_env(
                LANG="C.UTF-8",
                LC_ALL="C.UTF-8",
                NODE_ENV="production",
                MJML_BROWSER="1",
            ),
            timeout=MJML_TIMEOUT_SECONDS,
        )
    except _node.NodeError as exc:
        raise RuntimeError(f"MJML renderer unavailable: {exc}") from exc
    if completed.returncode:
        detail = completed.stderr.strip()[-2000:] or f"exit {completed.returncode}"
        raise RuntimeError(f"MJML render failed: {detail}")
    rendered = completed.stdout
    if "<html" not in rendered.lower() or "</html>" not in rendered.lower():
        raise RuntimeError("MJML renderer returned an incomplete document")
    return rendered.rstrip() + "\n"


@cache
def _markdown():
    """One lazy, inert CommonMark parser; MJML owns presentation."""
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")
    parser.validateLink = lambda url: bool(re.match(r"^https?://", url, re.IGNORECASE))
    for rule in ("image", "fence"):
        parser.renderer.rules[rule] = lambda *_: ""
    return parser


def inline_html(text: str) -> str:
    return _markdown().renderInline(str(text))


_CJK_PER_MINUTE = 330
_WORDS_PER_MINUTE = 220
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
_TAGS = re.compile(r"<[^>]+>")


def reading_minutes(html: str) -> int:
    text = _TAGS.sub(" ", html)
    cjk = len(_CJK.findall(text))
    latin = len([word for word in re.sub(_CJK.pattern, " ", text).split() if word.strip()])
    minutes = cjk / _CJK_PER_MINUTE + latin / _WORDS_PER_MINUTE
    return max(1, round(minutes)) if cjk or latin else 0


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
    """Build the semantic document in Python, then delegate email layout to MJML."""
    overview, demoted = normalize_decisions(overview or {})
    context = context or {}
    counts = manifest.get("counts", {})
    scan: list[MJNode] = [_render_masthead(manifest, context)]
    meta_bits = [f"{counts.get('bytes', 0) // 1024} KB 源文本"]
    generated = str(manifest.get("generated", ""))
    if counts.get("updates"):
        meta_bits.append(f"{counts['updates']} 条状态更新")
    if generated:
        meta_bits.append(f"生成于 {html_mod.escape(generated[11:16] or generated)}")
    if context.get("weather"):
        meta_bits.append("天气 Open-Meteo，地点取自当天日程")

    health = manifest.get("health") or {}
    if health:
        signals = _render_signals(health, brief, overview)
        if signals:
            scan.append(signals)
        meta_bits.extend(_fleet_bits(health, brief))
    scan.extend(_render_quota(context))
    if context.get("warnings"):
        scan.append(
            _section(
                [_text(f'<p class="small warning">{"<br>".join(f"! {html_mod.escape(str(w))}" for w in context["warnings"])}</p>')],
                padding="8px 20px 0",
            )
        )
    if brief:
        scan.append(_render_brief(brief))
    if manifest.get("updates"):
        scan.append(_render_updates(manifest["updates"]))
    if manifest.get("update_warnings"):
        scan.append(
            _section(
                [_text(f'<p class="small warning">{"<br>".join(f"! {html_mod.escape(str(w))}" for w in manifest["update_warnings"])}</p>')]
            )
        )
    if overview.get("headline"):
        scan.append(
            _section([_text(f'<p class="lead">{inline_html(overview["headline"])}</p>')], padding="20px 20px 0")
        )
    sections = overview.get("sections") or []
    if sections:
        scan.extend(_render_section(section, manifest) for section in sections)
    else:
        scan.append(_section([_text('<p class="small">No overview supplied; source index only.</p>')]))
    frontier = _render_frontier(
        overview.get("frontier_labs"), str(manifest.get("window", {}).get("until", ""))
    )
    if frontier:
        scan.append(frontier)

    routine_briefs = _render_routine_briefs(overview.get("routines") or [], manifest)
    if routine_briefs:
        scan.append(
            _section([_heading("routine 摘要"), *routine_briefs], padding="18px 20px 0")
        )
    articles = _render_articles(overview.get("articles") or [])
    if articles:
        scan.append(
            _section(
                [_heading("新文章", _articles_badge(overview.get("articles") or [])), *articles],
                padding="18px 20px 0",
            )
        )

    depth: list[MJNode] = []
    curated = _render_deep_read_curated(overview.get("deep_read"))
    feed_items = _render_feed_items(manifest) if curated else []
    if curated:
        deep_nodes = [*curated, *feed_items]
        badge = _deep_read_badge(overview.get("deep_read"), manifest)
    else:
        try:
            deep_nodes = _render_deep_read(manifest, vault_root())
        except PathsError:
            deep_nodes = _render_deep_read(manifest)
        badge = ""
    if deep_nodes:
        heading = _heading(
            "情报详读", badge + _cost_badge(reading_minutes(_node_text(deep_nodes)))
        )
        children = [heading]
        gap = deep_read_lane_gap(overview.get("deep_read"), manifest)
        if gap:
            children.append(_text(f'<p class="small warning">! {html_mod.escape(gap)}</p>'))
        children.extend(deep_nodes)
        depth.append(_section(children, padding="18px 20px 0"))

    retro = _render_retrospect(retrospect or [])
    if retro:
        depth.append(
            _section(
                [_heading("随机回顾", _cost_badge(reading_minutes(_node_text(retro)))), *retro],
                padding="18px 20px 0",
            )
        )
    index_children = [_heading("来源索引 / Source index")]
    if not any(True for _ in iter_sources(manifest)):
        index_children.append(_text('<p class="small">No routine output in this window.</p>'))
    for lane in manifest.get("lanes", []):
        items = "".join(_render_source(source) for source in lane.get("sources", []))
        index_children.append(
            _text(
                f'<h3 class="index-title">{html_mod.escape(str(lane.get("lane", "")))} '
                f'({lane.get("files", 0)})</h3><ul class="source-index">{items}</ul>'
            )
        )
    background = _background_sources(manifest)
    if background:
        index_children.append(_text(
            '<h3 class="index-title">背景来源 / Background context</h3><ul class="source-index">'
            + "".join(_render_source(source) for source in background) + "</ul>"
        ))
    depth.append(_section(index_children, padding="18px 20px 0"))
    if manifest.get("truncated"):
        depth.append(_section([_text('<p class="small">Selection was truncated by --max-files; narrow the window.</p>')]))
    if manifest.get("skipped_routines"):
        skipped = ", ".join(html_mod.escape(str(value)) for value in manifest["skipped_routines"])
        depth.append(_section([_text(f'<p class="note">Excluded from this digest: {skipped}.</p>')]))
    gaps = [str(g).strip() for g in (overview.get("gaps") or []) if str(g).strip()]
    gaps.extend(demoted)
    gaps.extend(str(warning) for warning in manifest.get("context_warnings") or [])
    for _, source in iter_sources(manifest):
        meta = source.get("meta") or {}
        if str(meta.get("status", "")).casefold() in {"degraded", "partial", "failed", "blocked", "error"}:
            details = [f'{source.get("label") or source.get("path")}: status={meta["status"]}']
            if meta.get("channels_reached"):
                details.append(f'channels_reached={meta["channels_reached"]}')
            if source.get("excerpt"):
                details.append(str(source["excerpt"]))
            gaps.append(" · ".join(details))
    note_gap = feed_note_gap(manifest)
    if note_gap:
        gaps.append(note_gap)
    if gaps:
        depth.append(
            _section([_text(f'<p class="note">输入缺口<br>{"<br>".join(html_mod.escape(g) for g in gaps)}</p>')])
        )
    depth.append(
        _section(
            [
                _node("mj-divider", attrs={"padding": "0 0 12px", "border-width": "1px", "border-color": _RULE}, content=""),
                _text(
                    f'<p class="colophon">{" · ".join(meta_bits)}<br>'
                    'Generated by <code>scripts/routine_digest.py</code> from '
                    '<code>$OV/_meta/routine_watch.toml</code> and optional '
                    '<code>$OV/_meta/digest_updates.toml</code>. Paths are relative to the vault root.</p>'
                ),
            ],
            padding="18px 20px 28px",
        )
    )

    fold = _section(
        [
            _text(
                f'<p class="fold">以上 {reading_minutes(_node_text(scan))} 分钟读完 · '
                f'以下 {reading_minutes(_node_text(depth))} 分钟，按需</p>'
            )
        ],
        padding="20px 20px 0",
    )
    title = digest_title(manifest)
    document = _node(
        "mjml",
        attrs={"lang": "zh-CN", "dir": "ltr"},
        children=[
            _node(
                "mj-head",
                children=[
                    _node("mj-title", content=html_mod.escape(title)),
                    _node("mj-preview", content=html_mod.escape(str(overview.get("headline") or title))),
                    _node(
                        "mj-attributes",
                        children=[
                            _node("mj-all", attrs={"font-family": _FONT, "color": _INK}, content=""),
                            _node("mj-text", attrs={"font-size": "15px", "line-height": "1.65", "color": _INK}, content=""),
                        ],
                    ),
                    _node("mj-style", attrs={"inline": "inline"}, content=_CSS),
                ],
            ),
            _node(
                "mj-body",
                attrs={"width": "640px", "background-color": "#ffffff"},
                children=[*scan, fold, *depth],
            ),
        ],
    )
    return _compile_mjml(document)


def _cost_badge(minutes: int) -> str:
    return f'<span class="cost">{minutes} 分钟</span>' if minutes else ""


def _render_masthead(manifest: dict[str, Any], context: dict[str, Any]) -> MJNode:
    title = digest_title(manifest)
    head, sep, tail = title.partition(" — ")
    title_html = html_mod.escape(title)
    if sep:
        title_html = f'{html_mod.escape(head)} <span class="digest-date">{html_mod.escape(tail)}</span>'
    weather_html = ""
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
        hours = []
        for row in weather.get("hours") or []:
            if not isinstance(row, dict) or "hour" not in row or "temp" not in row:
                continue
            try:
                hour = int(row["hour"])
            except (TypeError, ValueError):
                continue
            hours.append(f'{hour}:00 {html_mod.escape(str(row["temp"]))}°')
        sub = " · ".join([value for value in (" · ".join(hours), str(weather.get("date") or "")) if value])
        weather_html = f'<div class="weather">{line}'
        if sub:
            weather_html += f'<span class="weather-sub">{html_mod.escape(sub)}</span>'
        weather_html += "</div>"
    columns = [_column([_text(f'<h1 class="digest-title">{title_html}</h1>')], width="65%", valign="bottom")]
    if weather_html:
        columns.append(_column([_text(weather_html, align="right")], width="35%", valign="bottom"))
    return _node(
        "mj-section",
        attrs={"padding": "24px 20px 12px", "border-bottom": f"2px solid {_INK}"},
        children=[_node("mj-group", children=columns)],
    )


_HEADING_OVERDUE = re.compile(r"(\d+) 条逾期")
_FIRST_INT = re.compile(r"(\d+)")


def _brief_counts(brief: dict[str, Any] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in (brief or {}).get("groups") or []:
        if not isinstance(group, dict):
            continue
        heading = str(group.get("heading", ""))
        if group.get("kind") == "recurring":
            match = _HEADING_OVERDUE.search(heading)
            counts["recurring_overdue"] = int(match.group(1)) if match else 0
        elif group.get("kind") == "review":
            items = group.get("items") or []
            match = _FIRST_INT.search(heading)
            counts["review_debt"] = len(items) if items else (int(match.group(1)) if match else 0)
    return counts


WEIGHT_STALE_DAYS = 3
DECISION_SECTION = "需要的决策"
SIGNAL_SECTION = "信号"


def _decision_count(overview: dict[str, Any] | None) -> int:
    return sum(
        len(section.get("bullets") or [])
        for section in (overview or {}).get("sections") or []
        if isinstance(section, dict) and section.get("title") == DECISION_SECTION
    )


def decision_shape_missing(bullet: Any) -> list[str]:
    if not isinstance(bullet, dict):
        return ["text", "between", "settles"]
    missing = []
    if not str(bullet.get("text") or "").strip():
        missing.append("text")
    between = bullet.get("between")
    if not isinstance(between, list) or len([item for item in between if str(item or "").strip()]) < 2:
        missing.append("between")
    if not str(bullet.get("settles") or "").strip():
        missing.append("settles")
    return missing


def normalize_decisions(overview: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    sections = overview.get("sections")
    if not isinstance(sections, list):
        return overview, []
    demoted: list[str] = []
    moved: list[Any] = []
    new_sections: list[Any] = []
    for section in sections:
        if not (isinstance(section, dict) and section.get("title") == DECISION_SECTION):
            new_sections.append(section)
            continue
        updated = dict(section)
        kept = []
        for index, bullet in enumerate(section.get("bullets") or [], 1):
            missing = decision_shape_missing(bullet)
            if missing:
                moved.append(bullet)
                demoted.append(
                    f"{DECISION_SECTION} #{index} 缺 {'/'.join(missing)}, 已降级为{SIGNAL_SECTION}"
                )
            else:
                kept.append(bullet)
        updated["bullets"] = kept
        new_sections.append(updated)
    if not moved:
        return overview, []
    position = next(
        (index for index, section in enumerate(new_sections) if isinstance(section, dict) and section.get("title") == SIGNAL_SECTION),
        None,
    )
    if position is None:
        target: dict[str, Any] = {"title": SIGNAL_SECTION, "bullets": []}
        new_sections.append(target)
    else:
        target = dict(new_sections[position])
        new_sections[position] = target
    target["bullets"] = list(target.get("bullets") or []) + moved
    result = dict(overview)
    result["sections"] = new_sections
    return result, demoted


def _render_signals(
    health: dict[str, Any],
    brief: dict[str, Any] | None = None,
    overview: dict[str, Any] | None = None,
) -> MJNode | None:
    signals = (brief or {}).get("signals") or {}
    cells: list[tuple[str, str, str]] = []
    closing = int(signals.get("closing", 0) or 0)
    if closing:
        cells.append((str(closing), "关窗", _URGENT if int(signals.get("closing_now", 0) or 0) else _ACCENT))
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
    if health.get("state_unavailable"):
        cells.append(("?", "Prefect", _URGENT))
    if not cells:
        return None
    columns = [
        _column(
            [_text(f'<span class="signal-number" style="color:{colour};">{html_mod.escape(value)}</span><span class="signal-label">{html_mod.escape(label)}</span>', align="center")]
        )
        for value, label, colour in cells
    ]
    return _node(
        "mj-section",
        attrs={"padding": "12px 20px 0", "css-class": _S_STAT_TABLE},
        children=[_node("mj-group", children=columns)],
    )


def _fleet_bits(health: dict[str, Any], brief: dict[str, Any] | None) -> list[str]:
    bits = [
        f'routine {health.get("reported", 0)}/{health.get("declared", 0)} 有产出',
        f'{health.get("completed", 0)} 完成',
        "Prefect 状态不可用" if health.get("state_unavailable") else f'{health.get("failed", 0)} 失败',
        f'{health.get("review_debt", 0)} 待 review',
    ]
    extra = _brief_counts(brief)
    if "recurring_overdue" in extra:
        bits.append(f'recurring 逾期 {extra["recurring_overdue"]}')
    if "review_debt" in extra:
        bits.append(f'review 债 {extra["review_debt"]}')
    return bits


_QUOTA_COLOUR = {"ok": _OK, "low": _AMBER, "critical": _URGENT}


def _render_quota(context: dict[str, Any]) -> list[MJNode]:
    entries = [row for row in context.get("quota") or [] if isinstance(row, dict) and row.get("name")]
    if not entries:
        return []
    columns: list[MJNode] = []
    for entry in entries:
        left = max(0, min(100, int(entry.get("left_percent", 0))))
        colour = _QUOTA_COLOUR.get(str(entry.get("level", "")), _INK)
        age = entry.get("snapshot_age_hours")
        snap = f' · 快照 {html_mod.escape(str(age))}h 前' if age is not None else ""
        meta = (
            f'<p class="article-title">{html_mod.escape(str(entry["name"]))} '
            f'<span class="article-meta">{html_mod.escape(str(entry.get("window", "")))}</span></p>'
        )
        bar = _node(
            "mj-table",
            attrs={"padding": "7px 0 4px", "width": "100%"},
            content=(
                f'<tr><td width="{left}%" style="height:4px;background:{colour};font-size:0;">&nbsp;</td>'
                f'<td style="height:4px;background:{_BAR_TRACK};font-size:0;">&nbsp;</td></tr>'
            ),
        )
        tail = (
            f'<span style="color:{colour};font-weight:600;">剩 {left}%</span> · '
            f'{html_mod.escape(str(entry.get("reset_relative", "")))}{snap}'
        )
        columns.append(_column([_text(meta), bar, _text(f'<p class="small">{tail}</p>')]))
    return [
        _section([_text('<p class="eyebrow">Harness 额度</p>')], padding="14px 20px 0"),
        _node(
            "mj-section",
            attrs={"padding": "7px 20px 0"},
            children=[_node("mj-group", children=columns)],
        ),
    ]


_TRACE_STEM_CHARS = 22


def _trace_label(source: str) -> str:
    if not source:
        return ""
    name = source.rsplit("/", 1)[-1]
    stem, _, line = name.rpartition(":")
    if not stem:
        stem, line = name, ""
    stem = stem[:-3] if stem.endswith(".md") else stem
    if len(stem) > _TRACE_STEM_CHARS:
        stem = stem[: _TRACE_STEM_CHARS - 1] + "…"
    return html_mod.escape(f"{stem}:{line}" if line else stem)


def _due_cell(item: dict[str, Any], tier: Any) -> str:
    try:
        days = int(item["days_left"])
    except (KeyError, TypeError, ValueError):
        return f'<span style="color:{_FAINT};">·</span>'
    if days < 0:
        return f'<span style="color:{_URGENT};font-weight:600;">逾期 {-days}d</span>'
    if days == 0:
        return f'<span style="color:{_URGENT};font-weight:600;">今天</span>'
    if days == 1:
        return f'<span style="color:{_URGENT};font-weight:600;">明天</span>'
    return f'<span style="color:{_ACCENT if tier == 1 else _MUTED};">{days}d</span>'


def _render_brief(brief: dict[str, Any]) -> MJNode:
    groups = [group for group in brief.get("groups") or [] if isinstance(group, dict)]
    n_items = sum(len(group.get("items") or []) for group in groups)
    children = [
        _text(
            f'<p class="eyebrow">今日 <span class="eyebrow-meta">'
            f'{html_mod.escape(str(brief.get("date", "")))} · {n_items} 件</span></p>'
        )
    ]
    if not groups:
        children.append(_text("<p>今天没有关窗项、到期 TODO 或 review 债。</p>"))
    else:
        rows: list[str] = []
        for group in groups:
            group_class = "group-hot" if group.get("tier") == 1 else "group-cool"
            rows.append(
                f'<tr><td colspan="2" class="ledger-group"><span class="{group_class}">'
                f'{html_mod.escape(str(group.get("heading", "")))}</span></td></tr>'
            )
            for item in group.get("items") or []:
                if not isinstance(item, dict):
                    continue
                text = html_mod.escape(str(item.get("label") or item.get("text", "")))
                hint = str(item.get("hint") or "").strip()
                if hint:
                    text += f'<span class="hint">{html_mod.escape(hint)}</span>'
                source = str(item.get("source") or "")
                tail = f' <span class="trace">{_trace_label(source)}</span>' if source else ""
                if item.get("flag"):
                    flag = html_mod.escape(str(item["flag"]))
                    where = _trace_label(str(item.get("flag_source") or ""))
                    tail += f' <span class="flag">{flag}{" · " + where if where else ""}</span>'
                rows.append(
                    f'<tr class="ledger-row"><td class="ledger-due">{_due_cell(item, group.get("tier"))}</td>'
                    f'<td class="ledger-item">{text}{tail}</td></tr>'
                )
        children.append(
            _node("mj-table", attrs={"padding": "0", "width": "100%", "css-class": "digest-ledger"}, content="".join(rows))
        )
    warnings = brief.get("warnings") or []
    if warnings:
        children.append(
            _text(f'<p class="small warning">{"<br>".join(f"! {html_mod.escape(str(warning))}" for warning in warnings)}</p>')
        )
    return _section(children, padding="22px 20px 0")


def _render_updates(updates: list[dict[str, Any]]) -> MJNode:
    rows = []
    for item in updates:
        fields = " · ".join(
            f"{html_mod.escape(str(key))}: {inline_html(str(value))}"
            for key, value in (item.get("values") or {}).items()
        )
        rows.append(
            f'<li><strong>{html_mod.escape(str(item.get("label", "")))} · '
            f'{html_mod.escape(str(item.get("date", "")))}</strong><br>{fields} '
            f'<code>{html_mod.escape(str(item.get("path", "")))}</code></li>'
        )
    return _section([_heading("状态更新"), _text(f'<ul class="digest-list">{"".join(rows)}</ul>')], padding="18px 20px 0")


def _render_routine_briefs(briefs: list[dict[str, Any]], manifest: dict[str, Any]) -> list[MJNode]:
    caps = {source["path"]: int(source.get("max_lines") or DEFAULT_ROUTINE_LINES) for _, source in iter_sources(manifest)}
    labels = {source["path"]: str(source.get("label", "")) for _, source in iter_sources(manifest)}
    entries: list[MJNode] = []
    for brief in briefs:
        if not isinstance(brief, dict):
            continue
        path = str(brief.get("path") or "")
        lines = [line.strip() for line in str(brief.get("summary") or "").splitlines() if line.strip()]
        if not lines:
            continue
        cap = caps.get(path, DEFAULT_ROUTINE_LINES)
        clipped = len(lines) > cap
        tail = f'<span class="trace"> 已截至 {cap} 行</span>' if clipped else ""
        body = "<br>".join(inline_html(line) for line in lines[:cap])
        entries.append(
            _text(
                f'<div class="article"><p class="deep-title">{html_mod.escape(labels.get(path) or Path(path).name)}{tail}</p>'
                f'<p class="article-abstract">{body}</p></div>'
            )
        )
    return entries


def _safe_url(value: Any) -> str:
    raw = str(value or "").strip()
    return html_mod.escape(raw, quote=True) if re.match(r"^https?://", raw, re.IGNORECASE) else ""


def _article_duration(value: Any) -> tuple[str, float | None]:
    """Preserve Reader's duration label; total only recognized hours/minutes."""
    label = str(value or "").strip()
    match = re.fullmatch(
        r"(?:(\d+(?:\.\d+)?)\s*(?:h|hrs?|hours?)\s*)?(?:(\d+(?:\.\d+)?)\s*(?:m|mins?|minutes?)?)?",
        label, re.IGNORECASE,
    )
    minutes = float(match[1] or 0) * 60 + float(match[2] or 0) if match and label else None
    if re.fullmatch(r"\d+(?:\.\d+)?", label):
        label += " min"
    return label, minutes


def _render_articles(articles: list[dict[str, Any]]) -> list[MJNode]:
    entries: list[MJNode] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = html_mod.escape(str(article.get("title") or "")).strip()
        abstract = str(article.get("abstract") or "").strip()
        if not title or not abstract:
            continue
        url = _safe_url(article.get("url"))
        head = f'<a href="{url}">{title}</a>' if url else title
        meta_bits = []
        duration, _ = _article_duration(article.get("minutes"))
        if duration:
            meta_bits.append(html_mod.escape(duration))
        if article.get("source"):
            meta_bits.append(html_mod.escape(str(article["source"])))
        meta = f'<span class="article-meta">{" · ".join(meta_bits)}</span>' if meta_bits else ""
        why = str(article.get("why") or "").strip()
        why_html = f'<p class="article-why">{inline_html(why)}</p>' if why else ""
        entries.append(
            _text(
                f'<div class="article"><p class="article-title">{head}</p>{meta}{why_html}'
                f'<p class="article-abstract">{html_mod.escape(abstract)}</p></div>'
            )
        )
    return entries


def _articles_badge(articles: list[dict[str, Any]]) -> str:
    shown = [article for article in articles if isinstance(article, dict) and str(article.get("title") or "").strip() and str(article.get("abstract") or "").strip()]
    if not shown:
        return ""
    durations = [_article_duration(article.get("minutes"))[1] for article in shown]
    minutes = sum(value or 0 for value in durations) if None not in durations else 0
    bits = [f"{len(shown)} 篇"] + ([f"{minutes:g} 分钟"] if minutes else [])
    return f'<span class="section-count">{" · ".join(bits)}</span>'


def _render_frontier(labs: Any, digest_date: str) -> MJNode | None:
    if not isinstance(labs, dict):
        return None
    signals = [row for row in labs.get("signals") or [] if isinstance(row, dict) and row.get("lab") and row.get("text")]
    drift = int(labs.get("drift_count") or 0)
    promotions = int(labs.get("promotion_count") or 0)
    sweep = str(labs.get("sweep_date") or "")
    bits = [f"{len(signals)} 条信号", f"{drift} 漂移", f"{promotions} 晋级"]
    if sweep:
        bits.append(f"扫描 {sweep[5:] or sweep}")
    children = [_heading("前沿实验室", f'<span class="section-count">{html_mod.escape(" · ".join(bits))}</span>')]
    if not signals and not labs.get("watchlist_note"):
        return _section(children, padding="18px 20px 0")
    if sweep and (not digest_date or not _within_days(sweep, digest_date, 1)):
        children.append(_text(f'<p class="small">本期扫描 {html_mod.escape(sweep)} 已随当日 digest 报告，之后尚无新扫描。</p>'))
        return _section(children, padding="18px 20px 0")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        grouped.setdefault(str(signal["lab"]), []).append(signal)
    fragments = []
    for lab, rows in grouped.items():
        fragments.append(f'<p class="lab-name">{html_mod.escape(lab)}</p><ul class="digest-list">')
        for signal in rows:
            category = html_mod.escape(str(signal.get("category") or ""))
            if signal.get("tier"):
                tier = f'{html_mod.escape(str(signal["tier"]))}级来源'
                category = f"{category} · {tier}" if category else tier
            chip = f'<span class="lab-category">{category}</span> ' if category else ""
            url = _safe_url(signal.get("url"))
            link = f' <a href="{url}">来源 ↗</a>' if url else ""
            fragments.append(f"<li>{chip}{inline_html(str(signal['text']))}{link}</li>")
        fragments.append("</ul>")
    note = str(labs.get("watchlist_note") or "").strip()
    if note:
        fragments.append(f'<p class="note">{inline_html(note)}</p>')
    children.append(_text("".join(fragments)))
    return _section(children, padding="18px 20px 0")


def _render_deep_read_curated(deep_read: Any) -> list[MJNode]:
    if not isinstance(deep_read, dict):
        return []
    entries = [row for row in deep_read.get("entries") or [] if isinstance(row, dict) and row.get("title") and (row.get("facts") or row.get("why"))]
    if not entries:
        return []
    total = deep_read.get("total")
    label = f"信号精选 · {len(entries)}" + (f" / {total}" if total else "")
    nodes = [_text(f'<p class="eyebrow">{html_mod.escape(label)}</p>')]
    for entry in entries:
        url = _safe_url(entry.get("url"))
        link = f' <a href="{url}">来源 ↗</a>' if url else ""
        facts = [str(fact) for fact in entry.get("facts") or [] if str(fact).strip()][:2]
        fact_html = f'<ul class="digest-list">{"".join(f"<li>{inline_html(fact)}</li>" for fact in facts)}</ul>' if facts else ""
        why = str(entry.get("why") or "").strip()
        why_html = f'<p class="deep-why">{inline_html(why)}</p>' if why else ""
        nodes.append(
            _text(
                f'<div class="deep-item"><p class="deep-title">{html_mod.escape(str(entry["title"]))}{link}</p>'
                f"{fact_html}{why_html}</div>"
            )
        )
    return nodes


def _render_feed_items(manifest: dict[str, Any]) -> list[MJNode]:
    rows = []
    for lane, source in iter_sources(manifest):
        if lane != "Tech feed":
            continue
        for item in source.get("items") or []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            url = _safe_url(item.get("url"))
            head = f'<a href="{url}" style="font-weight:600;">{html_mod.escape(title)}</a>' if url else f'<span style="font-weight:600;">{html_mod.escape(title)}</span>'
            note = str(item.get("note") or "").strip()
            tail = f'<br><span class="feed-note">{html_mod.escape(note)}</span>' if note else ""
            rows.append(f'<tr class="feed-item"><td style="padding:9px 0;border-top:1px solid {_RULE};">{head}{tail}</td></tr>')
    if not rows:
        return []
    return [
        _text(f'<p class="eyebrow">科技动态 · {len(rows)}</p>'),
        _node("mj-table", attrs={"padding": "0", "width": "100%"}, content="".join(rows)),
    ]


def _feed_note_stats(items: list[dict[str, Any]]) -> tuple[int, int, int]:
    return (
        len(items),
        sum(1 for item in items if str(item.get("note") or "").strip()),
        sum(1 for item in items if _CJK.search(str(item.get("note") or ""))),
    )


def _feed_note_message(total: int, noted: int, cjk: int) -> str | None:
    if not total or cjk == total:
        return None
    return (
        f"科技动态 {total} 条中 {noted} 条有摘要, {cjk} 条为中文; "
        "资讯例程需按「链接下一行缩进、中文、一句话」写摘要"
    )


def feed_note_gap(manifest: dict[str, Any]) -> str | None:
    items = [
        item
        for lane, source in iter_sources(manifest)
        if lane == "Tech feed"
        for item in source.get("items") or []
        if isinstance(item, dict)
    ]
    return _feed_note_message(*_feed_note_stats(items))


_LEDGER_ROW = re.compile(
    r'<tr class="ledger-row">\s*<td class="ledger-due"[^>]*>(?P<due>.*?)</td>\s*'
    r'<td class="ledger-item"[^>]*>(?P<item>.*?)</td>',
    re.S,
)
_FEED_ITEM = re.compile(r'<tr class="feed-item">(?P<row>.*?)</tr>', re.S)
_FEED_NOTE = re.compile(r'<span class="feed-note"[^>]*>(?P<note>.*?)</span>', re.S)
_DECISION = re.compile(r'<li class="decision"[^>]*>(?P<card>.*?)</li>', re.S)
_DECISION_OPTION = re.compile(r'<span class="decision-option-text">(?P<text>.*?)</span>', re.S)
_DECISION_SETTLES = re.compile(r'<span class="decision-settles">(?P<text>.*?)</span>', re.S)


def _countdown_repeated(due: str, item: str) -> bool:
    if re.fullmatch(r"(?:逾期 )?\d+d", due):
        return re.search(rf"(?<!\d){re.escape(due)}(?!\d)", item) is not None
    return re.search(rf"(?:^|·)\s*{re.escape(due)}\s*·", item) is not None


def check_html(document: str) -> list[str]:
    findings: list[str] = []
    for match in _LEDGER_ROW.finditer(document):
        due = _TAGS.sub("", match.group("due")).strip()
        item = " ".join(_TAGS.sub(" ", match.group("item")).split())
        if due and due != "·" and _countdown_repeated(due, item):
            findings.append(f"倒计时「{due}」在条目文本里重复: {item[:60]}")
    rows = _FEED_ITEM.findall(document)
    if rows:
        notes = [_FEED_NOTE.search(row) for row in rows]
        noted = sum(1 for note in notes if note and note.group("note").strip())
        cjk = sum(1 for note in notes if note and _CJK.search(note.group("note")))
        message = _feed_note_message(len(rows), noted, cjk)
        if message:
            findings.append(message)
    for index, match in enumerate(_DECISION.finditer(document), 1):
        card = match.group("card")
        options = [_TAGS.sub("", option.group("text")).strip() for option in _DECISION_OPTION.finditer(card)]
        if len([option for option in options if option]) < 2:
            findings.append(f"{DECISION_SECTION} #{index} 选项不足两个")
        settles = _DECISION_SETTLES.search(card)
        settle_text = _TAGS.sub("", settles.group("text")) if settles else ""
        settle_text = re.sub(r"^\s*·\s*截止.*$", "", settle_text).strip()
        if not settle_text:
            findings.append(f"{DECISION_SECTION} #{index} 缺定案条件")
    return findings


def _deep_read_badge(deep_read: Any, manifest: dict[str, Any]) -> str:
    bits = []
    if isinstance(deep_read, dict):
        entries = deep_read.get("entries") or []
        total = deep_read.get("total")
        if entries:
            bits.append(f"信号 {len(entries)}" + (f" / {total}" if total else ""))
    feed = sum(len(source.get("items") or []) for lane, source in iter_sources(manifest) if lane == "Tech feed")
    if feed:
        bits.append(f"科技动态 {feed}")
    return f'<span class="section-count">{html_mod.escape(" · ".join(bits))}</span>' if bits else ""


def _background_sources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Index-only references, deduplicated against fresh paths and anchors."""
    fresh = [source for _, source in iter_sources(manifest)]
    paths = {source.get("path") for source in fresh}
    anchors = {source.get("anchor") for source in fresh}
    background = []
    for source in (manifest.get("context_sources") or {}).values():
        if not isinstance(source, dict) or not source.get("path"):
            continue
        if source["path"] in paths or (source.get("anchor") and source["anchor"] in anchors):
            continue
        background.append(source)
        paths.add(source["path"])
        anchors.add(source.get("anchor"))
    return background


def _render_section(section: dict[str, Any], manifest: dict[str, Any]) -> MJNode:
    if not isinstance(section, dict):
        return _section([_text('<p class="small">(malformed overview section skipped)</p>')])
    known_paths = {source["path"] for _, source in iter_sources(manifest)}
    known_paths.update(source["path"] for source in (manifest.get("context_sources") or {}).values()
                       if isinstance(source, dict) and source.get("path"))
    bullets = section.get("bullets") or []
    body = ""
    if section.get("note"):
        body += f'<p>{inline_html(section["note"])}</p>'
    if bullets:
        rendered = []
        if section.get("title") == DECISION_SECTION:
            rendered = [_render_decision_bullet(bullet, known_paths) for bullet in bullets]
        else:
            rendered = [f"<li>{_render_bullet(bullet, known_paths)}</li>" for bullet in bullets]
        body += f'<ul class="digest-list">{"".join(rendered)}</ul>'
    return _section(
        [_heading(str(section.get("title", "")), f'<span class="section-count">{len(bullets)}</span>'), _text(body)]
    )


def _render_decision_bullet(bullet: dict[str, Any], known_paths: set[str]) -> str:
    provenance = _render_bullet(dict(bullet, text=""), known_paths)
    question = inline_html(str(bullet.get("text") or ""))
    options = [str(option).strip() for option in bullet.get("between") or [] if str(option or "").strip()]
    keys = "ABCDEFG"
    option_html = "<br>".join(
        f'<span class="decision-option">{keys[index] if index < len(keys) else index + 1}</span>'
        f'<span class="decision-option-text">{inline_html(option)}</span>'
        for index, option in enumerate(options)
    )
    settle = inline_html(str(bullet.get("settles") or ""))
    if str(bullet.get("by") or "").strip():
        settle += f' · 截止 {html_mod.escape(str(bullet["by"]).strip())}'
    return (
        f'<li class="decision"><p class="decision-question">{question}</p>'
        f'<p class="decision-options">{option_html}</p>'
        f'<p class="decision-settle">定案 · <span class="decision-settles">{settle}</span>{provenance}</p></li>'
    )


def _render_bullet(bullet: Any, known_paths: set[str]) -> str:
    if not isinstance(bullet, dict):
        return inline_html(str(bullet))
    body = inline_html(str(bullet.get("text", "")))
    tail = []
    url = _safe_url(bullet.get("url"))
    if url:
        tail.append(f'<a href="{url}">source</a>')
    for source in bullet.get("sources") or []:
        ref = str(source)
        label = html_mod.escape(Path(ref).name)
        tail.append(f"<code>{label}</code>" if ref in known_paths else f"{label} (unmatched)")
    return body + (f' <span class="small">{" · ".join(tail)}</span>' if tail else "")


DEEP_PER_SOURCE_CHARS = 6000
DEEP_TOTAL_BYTES = 90_000
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def markdown_to_html(text: str, *, limit: int = DEEP_PER_SOURCE_CHARS) -> tuple[str, bool]:
    body = _MD_IMAGE.sub("", strip_frontmatter(text))
    truncated = len(body) > limit
    if truncated:
        cut = body[:limit]
        newline = cut.rfind("\n")
        body = cut[:newline] if newline > limit * 0.6 else cut
    parser = _markdown()
    env: dict[str, Any] = {}
    tokens = parser.parse(body, env)
    if any(token.nesting == 1 and token.level >= parser.options["maxNesting"] - 1 for token in tokens):
        plain = html_mod.escape(body).replace("\n", "<br>")
        rendered = f'<p class="small">嵌套过深，以下按纯文本显示。</p><p>{plain}</p>'
    else:
        rendered = parser.renderer.render(tokens, parser.options, env)
    return f'<div class="markdown">{rendered}</div>', truncated


def _render_deep_read(manifest: dict[str, Any], ov: Path | None = None) -> list[MJNode]:
    entries: list[MJNode] = []
    budget = DEEP_TOTAL_BYTES
    for _, source in iter_sources(manifest):
        body_html = ""
        truncated = False
        if ov is not None:
            try:
                body_html, truncated = markdown_to_html(
                    (ov / str(source.get("path", ""))).read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                pass
        if not body_html:
            excerpt = str(source.get("excerpt") or "").strip()
            if not excerpt:
                continue
            body_html = f"<p>{html_mod.escape(excerpt)}</p>"
        content = (
            f'<div class="deep-item"><p class="deep-title">'
            f'{html_mod.escape(str(source.get("headline") or source.get("label", "")))}'
            f'<span class="deep-meta">{html_mod.escape(str(source.get("label", "")))} · '
            f'{html_mod.escape(str(source.get("date", "")))}</span></p>{body_html}'
        )
        if truncated:
            content += (
                '<p class="small">报告在此截断,完整版在 '
                f'<code>{html_mod.escape(str(source.get("path", "")))}</code></p>'
            )
        content += "</div>"
        size = len(content.encode("utf-8"))
        if size > budget:
            entries.append(_text('<p class="note">余下的报告未随信附上,以免整封被邮件客户端截断。见来源索引。</p>'))
            break
        budget -= size
        entries.append(_text(content))
    return entries


def _render_retrospect(picks: list[dict[str, Any]]) -> list[MJNode]:
    entries = []
    for pick in picks:
        if not pick.get("reviewed") or not str(pick.get("excerpt") or "").strip():
            continue
        days = int(pick.get("age_days") or 0)
        age = f"{days / 365:.1f} 年前" if days >= 365 else f"{days} 天前"
        entries.append(
            _text(
                f'<div class="deep-item"><p class="deep-title">{html_mod.escape(str(pick.get("title", "")))}'
                f'<span class="deep-meta">{html_mod.escape(age)} · {html_mod.escape(str(pick.get("tier", "")))}</span></p>'
                f'<p class="deep-body">{html_mod.escape(str(pick.get("excerpt", "")))}</p>'
                f'<p class="small"><code>{html_mod.escape(str(pick.get("path", "")))}</code></p></div>'
            )
        )
    return entries


_INDEX_ITEM_CAP = 5
_INDEX_EXCERPT_CAP = 180


def _render_source(source: dict[str, Any]) -> str:
    anchor = html_mod.escape(str(source.get("anchor", "")), quote=True)
    label = html_mod.escape(str(source.get("label", "")))
    when = html_mod.escape(str(source.get("date", "")))
    headline = html_mod.escape(str(source.get("headline", "")))
    head = f"<strong>{label}</strong> · {when}"
    if source.get("carried"):
        head += ' <span class="trace">补录</span>'
    if headline:
        head += f" · {headline}"
    if source.get("date_source"):
        head += f' <small>(date from {html_mod.escape(str(source["date_source"]))})</small>'
    lines = [f'<li id="{anchor}">{head}']
    units = source.get("units") or []
    items = source.get("items") or []
    urls = source.get("primary_urls") or []
    if units:
        rendered = []
        for unit in units:
            slug = html_mod.escape(humanize_slug(str(unit.get("slug", ""))))
            url = _safe_url(unit.get("source_url"))
            rendered.append(f'<a href="{url}">{slug}</a>' if url else slug)
        lines.append(f'<br>{" · ".join(rendered)}')
    elif items:
        rendered = []
        for item in items[:_INDEX_ITEM_CAP]:
            title = html_mod.escape(str(item.get("title") or item.get("url", "")))
            url = _safe_url(item.get("url"))
            rendered.append(f'<a href="{url}">{title}</a>' if url else title)
        tail = f" · +{len(items) - _INDEX_ITEM_CAP} more" if len(items) > _INDEX_ITEM_CAP else ""
        lines.append(f'<br>{" · ".join(rendered)}{tail}')
    elif urls:
        links = []
        for index, raw in enumerate(urls[:2]):
            url = _safe_url(raw)
            if url:
                links.append(f'<a href="{url}">primary {index + 1}</a>')
        if links:
            lines.append(f'<br>{" · ".join(links)}')
    elif source.get("excerpt"):
        excerpt = str(source["excerpt"])
        if len(excerpt) > _INDEX_EXCERPT_CAP:
            excerpt = excerpt[:_INDEX_EXCERPT_CAP].rstrip() + " …"
        lines.append(f"<br>{html_mod.escape(excerpt)}")
    lines.append(f'<br><code>{html_mod.escape(str(source.get("path", "")))}</code></li>')
    return "".join(lines)
