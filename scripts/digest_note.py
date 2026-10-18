"""Render one Reflect-native digest, screening untrusted text and ambiguous note links."""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _reflect import join_wrapped  # noqa: E402
from routine_digest_core import (  # noqa: E402
    DEFAULT_ROUTINE_LINES,
    _within_days,
    deep_read_lane_gap,
    iter_sources,
)

DECISION_SECTION = "需要的决策"
SIGNAL_SECTION = "信号"
_FACTS, _TRACE_STEM = 2, 22

_CJK = re.compile(r"[㐀-鿿぀-ヿ]")
_UNLINKABLE = re.compile(r"^/|//|\.md$|^\d{4}-\d{2}-\d{2}$")
_TOKEN = re.compile(
    r"(?P<code>(`+)(?!`).+?(?<!`)\2(?!`))"
    r"|(?P<wiki>!?\[\[(?P<target>[^\[\]\n|]*)(?:\|(?P<alias>[^\[\]\n]*))?\]\])"
    r"|(?P<img>!?)\[(?P<text>[^\[\]\n]*)\]\(\s*(?P<url>[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\s*\)"
)
_INERT = (  # anywhere in a field
    (re.compile(r"<br\s*/?>", re.I), " · "),            # ledger cell breaks
    # Reflect autolinks any bare `scheme://`; only http(s) may stay live (`reflect://` writes to the daily note).
    (re.compile(r"(?i)(?<![a-z0-9+.-])(?!https?:)([a-z][a-z0-9+.-]*):(?=//)"), "\\1∶"),
    (re.compile(r"<(?=[A-Za-z/!?]|$)"), "＜"),          # tags, comments, autolinks
    (re.compile(r"&(?=#?\w+;)"), "＆"),                  # entities
    (re.compile(r"(?<!\S)#(?=[\w-]*[^\W\d_]|[\w-]*$)"), "＃"),  # tags; a field's end may meet a letter
    (re.compile(r"\[(?=\[)"), "［"),                     # a leftover `[[` pairing with a later `]]`
    (re.compile(r"(?<!\S)\^(?=[A-Za-z0-9])"), "＾"),     # block ids
    (re.compile(r"\[(?=\^)"), "［"),                     # footnotes
    (re.compile(r"\]\("), "]（"),                        # a link the tokenizer refused
    (re.compile(r"%%"), "%％"),                          # Obsidian comments
    (re.compile(r"`"), "ˋ"),                             # an unpaired backtick
)
_LEAD = (  # at the start of a field, which may start a block or a list item
    (re.compile(r"^(?=([-*_=])[ \t]*(?:\1[ \t]*)+$)|^(?=~{3,}|\$\$)"), "·"),  # rule (an item's `- ` adds a mark), fence, math
    (re.compile(r"^>"), "＞"),                                                    # quote, callout
    (re.compile(r"^#"), "＃"),                                                    # heading
    (re.compile(r"^[-+*](?=\s|$)"), lambda m: "－＋＊"["-+*".index(m[0])]),        # list, `+ ` task
    (re.compile(r"^(\d{1,9})([.)])(?=\s|$)"), lambda m: m[1] + "．）"[".)".index(m[2])]),
    (re.compile(r"^\[(?=[ xX~/]\]|!|[^\]\n]*\]:)"), "［"),                        # task, callout, link definition
    (re.compile(r"^([A-Za-z][\w-]*):(?=:\s)"), r"\1∶"),                           # dataview field
)


def _one_line(value: Any) -> str:
    """Unescaped text on one physical line; CJK neighbours join without a space."""
    # U+FEFF is whitespace to Reflect's tag parser but not to Python, so it goes too.
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f﻿]", " ", html.unescape("" if value is None else str(value)))
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    return join_wrapped(lines, 0, len(lines)) if lines else ""


def _inert(text: str) -> str:
    for pattern, replacement in _INERT:
        text = pattern.sub(replacement, text)
    return text


def url(value: Any) -> str:
    """An http(s) destination safe inside `(...)`, else ''."""
    raw = html.unescape(str(value or "")).strip()
    if not re.fullmatch(r"(?i)https?://[^\s<>\"'`\\]+", raw):
        return ""
    return raw.translate(str.maketrans({"(": "%28", ")": "%29", "[": "%5B", "]": "%5D"}))


def _linked(text: str, target: str) -> str:
    """`[text](target)` for plain `text`; brackets in the text cannot close the link early."""
    text = text.replace("[", "［").replace("]", "］")
    return f"[{text}]({target})" if target and text else text


def md(value: Any, *, links: bool = True) -> str:
    """Screen one Markdown line, retaining code spans and only permitted HTTP(S) links."""
    text, out, last = _one_line(value), [], 0
    for match in _TOKEN.finditer(text):
        out.append(_inert(text[last:match.start()]))
        last = match.end()
        if match["code"]:
            out.append(match["code"])
        elif match["wiki"]:
            out.append(_inert(match["alias"] or match["target"]))
        elif not match["img"]:
            label = _inert(match["text"])
            out.append(_linked(label, url(match["url"])) if links else label)
    out.append(_inert(text[last:]))
    line = "".join(out).strip()
    for pattern, replacement in _LEAD:
        line = pattern.sub(replacement, line, count=1)
    return line


def plain(value: Any) -> str:
    return md(value, links=False)


def code(value: Any) -> str:
    text = _one_line(value)
    return f"`{text}`" if text and "`" not in text else plain(text)


def cite(path: Any, titles: Any = None) -> str:
    """`[[Title]]` when Reflect opens exactly this note by title, else the vault path in code."""
    rel = Path(str(path or "").strip())
    title = None
    if titles is not None and str(rel) not in ("", ".") and not rel.is_absolute() and ".." not in rel.parts:
        title = titles.link_title(rel)
    return f"[[{title}]]" if title and not _unlinkable(title) else code(path)


def _unlinkable(title: str) -> bool:
    """Titles Reflect resolves as paths, not names: a leading `/`, a URI, `.md`, a bare date, or a
    path-shaped title (`a/b`) whose last segment has an extension or any segment starts with `.`."""
    segments = title.split("/")
    path_shaped = len(segments) > 1 and all(s and s == s.strip() for s in segments)
    return bool(_UNLINKABLE.search(title)) or path_shaped and (
        segments[-1].rfind(".") > 0 or any(s.startswith(".") for s in segments))


def _int(value: Any, default: Any = 0) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mark(text: str, level: str) -> str:
    """`now` was red in the mail, `lead` its accent colour."""
    return f"=={text}==" if level == "now" else f"**{text}**" if level == "lead" else text


def _h2(title: str, *badges: str) -> str:
    return "## " + " · ".join([title or "…", *(badge for badge in badges if badge)])


def digest_title(manifest: dict[str, Any]) -> str:
    window = manifest.get("window") or {}
    since, until = window.get("since", "?"), window.get("until", "?")
    if manifest.get("selection") == "unacked":
        return f"Atelier Digest: backlog through {until}"
    if manifest.get("mode") == "daily" or since == until:
        return f"Atelier Daily: {until}"
    return f"Atelier Weekly: {since} → {until}"


def note_tag(manifest: dict[str, Any]) -> str:
    """The note's one Reflect tag, for filtering: weekly roll-ups `#周报`, daily and backlog `#日报`."""
    return "#周报" if digest_title(manifest).startswith("Atelier Weekly") else "#日报"


# ----- decisions -----

def decision_shape_missing(bullet: Any) -> list[str]:
    """Judged on the rendered text, so an option that neutralizes to nothing is no option."""
    if not isinstance(bullet, dict):
        return ["text", "between", "settles"]
    between = bullet.get("between")
    missing = [] if md(bullet.get("text")) else ["text"]
    if not isinstance(between, list) or sum(1 for option in between if md(option)) < 2:
        missing.append("between")
    return missing + ([] if md(bullet.get("settles")) else ["settles"])


def normalize_decisions(overview: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Incomplete decisions move to the first 信号 section; the caller's overview is untouched."""
    sections = overview.get("sections")
    if not isinstance(sections, list):
        return overview, []
    demoted: list[str] = []
    moved: list[Any] = []
    result: list[Any] = []
    number = 0  # across every decision section, so two sections never both say #1
    for section in sections:
        if not (isinstance(section, dict) and section.get("title") == DECISION_SECTION):
            result.append(section)
            continue
        kept = []
        for bullet in section.get("bullets") or []:
            number += 1
            if missing := decision_shape_missing(bullet):
                moved.append(bullet)
                demoted.append(f"{DECISION_SECTION} #{number} 缺 {'/'.join(missing)}，已降级为{SIGNAL_SECTION}")
            else:
                kept.append(bullet)
        result.append({**section, "bullets": kept})
    if not moved:
        return overview, []
    position = next((i for i, s in enumerate(result) if isinstance(s, dict) and s.get("title") == SIGNAL_SECTION), None)
    if position is None:
        result.append({"title": SIGNAL_SECTION, "bullets": moved})
    else:
        result[position] = {**result[position], "bullets": [*(result[position].get("bullets") or []), *moved]}
    return {**overview, "sections": result}, demoted


# ----- above the fold -----

def _masthead(context: dict, until: str) -> list[str]:
    blocks = []
    weather = context.get("weather")
    if isinstance(weather, dict) and weather.get("place"):
        bits = [f"**{plain(weather['place'])}** {plain(weather.get('tmin', '?'))}–{plain(weather.get('tmax', '?'))}°C",
                plain(weather.get("summary"))]
        if weather.get("precip_probability") is not None:
            bits.append(f"降水 {plain(weather['precip_probability'])}%")
        bits += [f"{_int(row['hour'])}:00 {plain(row['temp'])}°" for row in weather.get("hours") or []
                 if isinstance(row, dict) and _int(row.get("hour"), None) is not None and "temp" in row]
        if str(weather.get("date") or until) != until:
            bits.append(plain(weather["date"]))
        blocks.append(" · ".join(bit for bit in bits if bit))
    quota = []
    for row in context.get("quota") or []:
        if isinstance(row, dict) and (name := plain(row.get("name"))):
            left = max(0, min(100, _int(row.get("left_percent"))))
            level = {"critical": "now", "low": "lead"}.get(str(row.get("level")), "")
            reset = re.sub(r"(\d+) (天|小时|分钟)", lambda m: m[1] + {"天": "d", "小时": "h", "分钟": "m"}[m[2]],
                           plain(row.get("reset_relative"))).removesuffix("后重置").replace("重置时间未知", "未知")
            reset = re.sub(r"^(\d+)d (\d+)h$", lambda m: m[1] + "d" + (m[2] + "h" if m[2] != "0" else ""), reset)
            header = " · ".join(bit for bit in (name, plain(row.get("window"))) if bit)
            quota.append((header, _mark(str(left), level) + "%" + (f" / {reset}" if reset else "")))
    if quota:
        headers = ["额度", *(header for header, _ in quota)]
        table = [headers, ["---"] * len(headers), ["剩余 / 重置", *(value for _, value in quota)]]
        blocks += [_h2("模型额度"), "\n".join("| " + " | ".join(cell.replace("|", "｜") for cell in cells) + " |" for cells in table)]
    return blocks


def _due(item: dict[str, Any], tier: Any) -> str:
    days = _int(item.get("days_left"), None)
    if days is None:
        return ""
    if days < 0:
        return _mark(f"逾期 {-days}d", "now")
    if days <= 1:
        return _mark("今天" if days == 0 else "明天", "now")
    return _mark(f"{days}d", "lead" if tier == 1 else "")


def _trace(source: Any) -> str:
    """`stem:line` chip for a source Reflect cannot open by title."""
    name = str(source or "").rsplit("/", 1)[-1]
    stem, _, line = name.rpartition(":")
    stem, line = (stem, line) if stem else (name, "")
    stem = stem.removesuffix(".md")
    stem = stem if len(stem) <= _TRACE_STEM else stem[: _TRACE_STEM - 1] + "…"
    return code(f"{stem}:{line}" if line else stem) if stem else ""


def _refs(paths: list[Any], titles: Any) -> list[str]:
    """A section's one `来源` line of `[[Title]]`s, which Reflect previews on hover."""
    links = dict.fromkeys(link for path in paths if (link := cite(path, titles)).startswith("[["))
    return ["来源 " + " · ".join(links)] if links else []


def _brief(brief: dict[str, Any], titles: Any = None) -> list[str]:
    """Action groups are Outline targets; linkable sources leave the rows for one 来源 line."""
    groups = [group for group in brief.get("groups") or [] if isinstance(group, dict)]
    blocks, reminders = [], []
    if not groups:
        blocks = [_h2("今日"), "今天没有关窗项、到期 TODO 或 review 债。"]
    for group in groups:
        rows, paths = [], []
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            paths.append(path := re.sub(r":\d+$", "", str(item.get("source") or "")))
            trace = "" if cite(path, titles).startswith("[[") else _trace(item.get("source"))
            action = str(item.get("label") or item.get("text") or "").split(" — ", 1)[0]  # tails stay in the source
            parts = [_due(item, group.get("tier")), plain(action), trace]
            if flag := plain(item.get("flag")):
                parts.append(" ".join(bit for bit in (_mark(flag, "now"), _trace(item.get("flag_source"))) if bit))
            if not (parts := [part for part in parts if part]):
                continue  # an empty `- ` item is not native
            rows.append("- " + " · ".join(parts))
            rows += [f"  - {hint}"] if (hint := plain(item.get("hint"))) else []
        heading = plain(group.get("heading")) or "…"
        heading = re.sub(r" \((?:todos|recurring|deadlines)\.py (?:list|due)(?: [^()]*)?\)(?=$| · )| \([\w-]+\.md\)"
                         r"|(?<=项): [a-z_]+(?: · [a-z_]+)*", "", heading)
        if rows:
            blocks += [_h2(heading), "\n".join(rows), *_refs(paths, titles)]
        else:
            reminders.append(f"- {heading}")
    age = _int((brief.get("signals") or {}).get("weight_age_days"), None)
    if age is not None and age >= 0 and not any(re.search(rf"体重[^()]*\({age}d 前\)", block)
                                               for block in [*blocks, *reminders]):
        reminders.append(f"- 体重记录：{age}d 前")
    blocks += [_h2("其他提醒"), "\n".join(reminders)] if reminders else []
    return blocks + [f"! {text}" for w in brief.get("warnings") or [] if (text := plain(w)) and "-line cap" not in text]


def _updates(manifest: dict[str, Any], titles: Any) -> list[str]:
    rows = []
    for item in manifest.get("updates") or []:
        link = cite(item.get("path"), titles)
        head = [f"**{plain(item.get('label')) or '…'}**", plain(item.get("date")), link if link.startswith("[[") else ""]
        rows.append("- " + " · ".join(bit for bit in head if bit))
        for key, value in (item.get("values") or {}).items():
            name = plain(key)  # `[x]: …` would define a link reference
            rows.append(f"  - {'［' + name[1:] if name.startswith('[') else name}: {md(value)}".rstrip())
    warnings = [f"! {text}" for w in manifest.get("update_warnings") or [] if (text := plain(w))]
    if not rows and not warnings:
        return []
    return [_h2("状态更新", str(len(manifest.get("updates") or []))), *(["\n".join(rows)] if rows else []), *warnings]


def _provenance(bullet: dict[str, Any], known: set[str], titles: Any) -> list[str]:
    tail = [_linked("来源", url(bullet.get("url")))] if url(bullet.get("url")) else []
    for ref in map(str, bullet.get("sources") or []):
        tail.append(cite(ref, titles) if ref in known else f"{code(Path(ref).name)} (unmatched)")
    return tail


def _section(section: dict[str, Any], known: set[str], titles: Any) -> list[str]:
    bullets = section.get("bullets") or []
    blocks = [_h2(plain(section.get("title")), str(len(bullets)))]
    if note := md(section.get("note")):
        blocks.append(note)
    rows = []
    for bullet in bullets:
        if not isinstance(bullet, dict):
            rows += [f"- {text}"] if (text := md(bullet)) else []
        elif section.get("title") == DECISION_SECTION:
            settle = "定案：" + md(bullet.get("settles")) + (f" · 截止 {by}" if (by := plain(bullet.get("by"))) else "")
            options = [text for option in bullet.get("between") or [] if (text := md(option))]
            rows += [f"- {md(bullet.get('text'))}",
                     # Letters, never `8.`, which would open a third list level.
                     *(f"  - {chr(65 + i) + '.' if i < 26 else f'{i + 1}．'} {option}" for i, option in enumerate(options)),
                     "  - " + " · ".join([settle, *_provenance(bullet, known, titles)])]
        elif parts := [part for part in (md(bullet.get("text")), *_provenance(bullet, known, titles)) if part]:
            rows.append("- " + " · ".join(parts))
    return blocks + (["\n".join(rows)] if rows else [])


def _frontier(labs: Any, until: str) -> list[str]:
    """Full list on the sweep's day and the next; later days collapse to the counts."""
    if not isinstance(labs, dict):
        return []
    signals = [row for row in labs.get("signals") or [] if isinstance(row, dict) and row.get("lab") and md(row.get("text"))]
    raw = _one_line(labs.get("sweep_date"))
    sweep = plain(raw)  # the badge slices the raw date, then neutralizes what is left
    blocks = [_h2("前沿实验室", f"{len(signals)} 条信号", f"{_int(labs.get('drift_count'))} 漂移",
                  f"{_int(labs.get('promotion_count'))} 晋级", f"扫描 {plain(raw[5:]) or sweep}" if sweep else "")]
    note = md(labs.get("watchlist_note"))
    if (signals or note) and sweep and not (until and _within_days(sweep, until, 1)):
        return blocks + [f"本期扫描 {sweep} 已随当日 digest 报告，之后尚无新扫描。"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        grouped.setdefault(str(signal["lab"]), []).append(signal)
    for lab, rows in grouped.items():
        lines = []
        for row in rows:
            chip = [plain(row.get("category")), f"{plain(row['tier'])}级来源" if row.get("tier") else ""]
            link = _linked("来源 ↗", url(row.get("url"))) if url(row.get("url")) else ""
            lines.append("- " + " · ".join(bit for bit in (*chip, md(row["text"]), link) if bit))
        blocks += [f"**{plain(lab) or '…'}**", "\n".join(lines)]
    return blocks + ([note] if note else [])


def _routine_briefs(entries: list[Any], manifest: dict[str, Any], titles: Any) -> list[str]:
    sources = {source.get("path"): source for _, source in iter_sources(manifest)}
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        lines = [text for line in str(entry.get("summary") or "").splitlines() if (text := md(line))]
        if not lines:
            continue
        source = sources.get(path) or {}
        cap = _int(source.get("max_lines")) or DEFAULT_ROUTINE_LINES
        head = [f"**{plain(source.get('label') or Path(path).name) or '…'}**",
                f"已截至 {cap} 行" if len(lines) > cap else "", cite(path, titles) if path else ""]
        rows += ["- " + " · ".join(bit for bit in head if bit), *(f"  - {line}" for line in lines[:cap])]
    return [_h2("routine 摘要"), "\n".join(rows)] if rows else []


def _article_duration(value: Any) -> tuple[str, float | None]:
    """Keep Reader's label; total only recognized hours and minutes."""
    label = str(value if value is not None else "").strip()
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*(?:h|hrs?|hours?)\s*)?(?:(\d+(?:\.\d+)?)\s*(?:m|mins?|minutes?)?)?",
                         label, re.IGNORECASE)
    minutes = float(match[1] or 0) * 60 + float(match[2] or 0) if match and label else None
    return (label + " min" if re.fullmatch(r"\d+(?:\.\d+)?", label) else label), minutes


def _articles(articles: list[Any], titles: Any = None) -> list[str]:
    """Entries need a title and an abstract; the minutes badge only when every duration parsed."""
    rows, durations, sources = [], [], []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title, abstract = plain(article.get("title")), plain(article.get("abstract"))
        if not title or not abstract:
            continue
        label, minutes = _article_duration(article.get("minutes"))
        durations.append(minutes)
        head = _linked(title, url(article.get("url"))) if url(article.get("url")) else f"**{title}**"
        rows.append("- " + " · ".join(bit for bit in (head, plain(label), plain(article.get("source"))) if bit))
        rows += [f"  - 为何读：{why}"] if (why := md(article.get("why"))) else []
        rows.append(f"  - {abstract}")
        sources += article.get("sources") or []  # a vault note that already discusses it
    if not durations:
        return []
    total = sum(durations) if None not in durations else 0
    return [_h2("新文章", f"{len(durations)} 篇", f"{total:g} 分钟" if total else ""), "\n".join(rows),
            *_refs(sources, titles)]


# ----- below the fold -----

def _feed(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for lane, source in iter_sources(manifest) if lane == "Tech feed" and not source.get("empty")
            for item in source.get("items") or [] if isinstance(item, dict)]


def _feed_shown(manifest: dict[str, Any], titles: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(feed notes 科技动态 embeds, feed notes it links): a reader-ready report embeds; else titled items link every note."""
    fed = [s for lane, s in iter_sources(manifest) if lane == "Tech feed" and cite(s.get("path"), titles).startswith("[[")]
    ready = [s for s in fed if s.get("declared") and not s.get("empty")]
    if ready:
        return ready, []
    return [], fed if any(plain(item.get("title")) for item in _feed(manifest)) else []


def feed_note_gap(manifest: dict[str, Any]) -> str | None:
    """Every tech-feed item, titled or not, owes a Chinese one-line note."""
    items = _feed(manifest)
    noted = sum(1 for item in items if str(item.get("note") or "").strip())
    cjk = sum(1 for item in items if _CJK.search(str(item.get("note") or "")))
    if not items or cjk == len(items) or any(s.get("declared") for lane, s in iter_sources(manifest) if lane == "Tech feed"):
        return None  # an embedded reader section carries its own notes
    return f"科技动态 {len(items)} 条中 {noted} 条有摘要，{cjk} 条为中文；资讯例程需按「链接下一行缩进、中文、一句话」写摘要"


def _deep_read(deep_read: Any, manifest: dict[str, Any], titles: Any = None) -> list[str]:
    """Curated picks, then every titled tech-feed item from the manifest; never routine bodies."""
    deep = deep_read if isinstance(deep_read, dict) else {}
    entries = [e for e in deep.get("entries") or [] if isinstance(e, dict) and plain(e.get("title"))
               and (e.get("facts") or e.get("why"))]
    count = f"{len(entries)}" + (f" / {plain(deep['total'])}" if deep.get("total") else "")
    picks = []
    for entry in entries:
        link = _linked("来源 ↗", url(entry.get("url"))) if url(entry.get("url")) else ""
        picks.append("- " + " · ".join(bit for bit in (f"**{plain(entry['title'])}**", link) if bit))
        facts = [text for fact in entry.get("facts") or [] if (text := md(fact))][:_FACTS]
        picks += [f"  - {fact}" for fact in facts] + ([f"  - 为何重要：{why}"] if (why := md(entry.get("why"))) else [])
    feed = ["- " + (_linked(text, url(item.get("url"))) if url(item.get("url")) else f"**{text}**")
            for item in _feed(manifest) if plain(item.get("title")) and (text := plain(item.get("note")) or plain(item.get("title")))]
    embedded, linked = _feed_shown(manifest, titles)
    embeds = [f"!{cite(s.get('path'), titles)[:-2]}#{s['section']}]]" for s in embedded]
    body = ([_h2("信号精选", count), "\n".join(picks), *_refs([p for e in entries for p in e.get("sources") or []], titles)]
            if picks else []) + ([_h2("科技动态"), *embeds] if embeds else  # a reader-ready feed embeds as written
                                 [_h2("科技动态", str(len(feed))), "\n".join(feed), *_refs([s.get("path") for s in linked], titles)]
                                 if feed else [])
    gap = deep_read_lane_gap(deep_read, manifest)
    return [body[0], *([f"! {plain(gap)}"] if gap else []), *body[1:]] if body else []


def _sources(sources: list[Any], titles: Any, day: str) -> list[str]:
    """A day's notes embed: Reflect previews `![[Title]]` in place only as its own paragraph."""
    blocks, rows = [], []
    for source in (source for source in sources if not source.get("empty")):  # no update, no noise
        meta, linked = source.get("meta") or {}, cite(source.get("path"), titles)
        linked = linked if linked.startswith("[[") else ""  # a path is for agents, not this reader
        failed = {"degraded": "降级", "failed": "失败", "blocked": "阻塞", "error": "错误"}.get(
            str(meta.get("status", "")).casefold())  # a failed run is a finding; partial and carried are not
        flags = [bit for bit in (failed, plain(meta.get("channels_reached"))) if bit] if failed else []
        if day and linked:
            section = str(source.get("section") or "")  # a heading screening would alter matches nothing
            shown = linked[:-2] + "#" + section + "]]" if section and plain(section) == section else linked
            blocks += [*(["\n".join(rows)] if rows else []), *([" · ".join(flags)] if flags else []), "!" + shown]
            rows = []
        else:
            head = [f"**{plain(source.get('label')) or '…'}**", linked, *flags,
                    plain(source.get("date")) if source.get("date") != day else ""]
            rows.append("- " + " · ".join(bit for bit in head if bit))
    return blocks + (["\n".join(rows)] if rows else [])


def _keys(source: dict[str, Any]) -> set[str]:
    return {str(key) for key in (source.get("path"), source.get("anchor")) if key}


def _index(manifest: dict[str, Any], titles: Any) -> list[str]:
    """Daily notes embed their sources; a feed 科技动态 already links is not repeated."""
    fresh = [source for _, source in iter_sources(manifest)]
    blocks = [_h2("例程原文")] + ([] if fresh else ["No routine output in this window."])
    window = manifest.get("window") or {}
    day = str(window.get("until", "")) if window.get("since") == window.get("until") else ""
    for lane in manifest.get("lanes") or []:
        name = str(lane.get("lane", ""))
        sources = lane.get("sources") or []
        if name == "Tech feed":  # 科技动态 already embeds or links these
            shown = {s.get("path") for group in _feed_shown(manifest, titles) for s in group}
            sources = [s for s in sources if s.get("path") not in shown]
        blocks += [f"**{plain(name) or '…'}**", *shown] if (shown := _sources(sources, titles, day)) else []
    seen = {key for source in fresh for key in _keys(source)}
    background = []
    for source in (manifest.get("context_sources") or {}).values():
        if isinstance(source, dict) and source.get("path") and not _keys(source) & seen:
            background.append(source)
            seen |= _keys(source)
    blocks += ["**背景来源**", *_sources(background, titles, day)] if background else []
    if manifest.get("truncated"):
        blocks.append("Selection was truncated by --max-files; narrow the window.")
    return blocks


def _gaps(manifest: dict[str, Any], overview: dict[str, Any], notes: list[str]) -> list[str]:
    """Coverage flags live on source rows; only other input gaps belong here."""
    lines = [str(gap) for gap in overview.get("gaps") or []] + notes
    lines += [str(warning) for warning in manifest.get("context_warnings") or []]
    lines += [gap] if (gap := feed_note_gap(manifest)) else []
    day = len({(manifest.get("window") or {}).get(key) for key in ("since", "until")}) == 1  # only a day's note embeds
    lines += [f"{s.get('label') or s.get('path')} {s['breach']}" for _, s in iter_sources(manifest) if day and s.get("breach")]
    rows = list(dict.fromkeys(f"- {text}" for line in lines if (text := plain(line))))
    return ["**输入缺口**", "\n".join(rows)] if rows else []


def _colophon(manifest: dict[str, Any], context: dict) -> list[str]:
    bits = []
    if generated := str(manifest.get("generated") or ""):
        bits.append(f"生成于 {plain(generated[11:16] or generated)}")
    ages = dict.fromkeys((plain(q.get("name")), plain(q.get("snapshot_age_hours")).removesuffix(".0"))
                         for q in context.get("quota") or [] if isinstance(q, dict) and plain(q.get("name")))
    common = {age for _, age in ages}
    if any(common):
        bits.append("额度快照 " + (f"{next(iter(common))}h前" if len(common) == 1 else
                               "；".join(f"{name} {age}h前" for name, age in ages if age)))
    return ["---", " · ".join([*bits, note_tag(manifest)])]


# ----- the note -----

def render(
    manifest: dict[str, Any],
    overview: dict[str, Any] | None = None,
    brief: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    *,
    titles: Any = None,
) -> str:
    """The whole note; `overview is None` is the model-free render (frontmatter `curated: false`)."""
    curated = overview is not None
    overview, notes = normalize_decisions(overview or {})
    context = context or {}
    until = str((manifest.get("window") or {}).get("until", ""))
    known = {s["path"] for _, s in iter_sources(manifest) if s.get("path")}
    known |= {s["path"] for s in (manifest.get("context_sources") or {}).values() if isinstance(s, dict) and s.get("path")}
    scan = [*_masthead(context, until), *(_brief(brief, titles) if brief else []),
            *_updates(manifest, titles)]
    if headline := md(overview.get("headline")):
        scan.append(f"> {headline}")
    for number, section in enumerate(overview.get("sections") or [], 1):
        if isinstance(section, dict):
            scan += _section(section, known, titles)
        else:
            notes.append(f"overview section #{number} malformed; skipped")
    sweep = ((manifest.get("context_sources") or {}).get("frontier_labs") or {}).get("path")
    scan += [*frontier, *_refs([sweep], titles)] if (frontier := _frontier(overview.get("frontier_labs"), until)) else []
    scan += _routine_briefs(overview.get("routines") or [], manifest, titles)
    scan += _articles(overview.get("articles") or [], titles)
    notes += [str(warning) for warning in context.get("warnings") or []]  # a missing input changes no plan
    depth = [*_deep_read(overview.get("deep_read"), manifest, titles), *_index(manifest, titles),
             *_gaps(manifest, overview, notes), *_colophon(manifest, context)]
    head = f"---\ncurated: {'true' if curated else 'false'}\n---"
    return "\n\n".join([head, f"# {digest_title(manifest)}", *scan, "---", *depth]) + "\n"
