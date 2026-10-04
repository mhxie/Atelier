"""Reflect note titles and `[[Title]]` resolution over a vault.

Mirrors reflect-open's note claims: a key is answered by the first tier that
claims it (daily date, exact title, exact alias, emoji-folded title, emoji-folded
alias, then filename stem), and a key two notes claim in that tier is ambiguous. A note's title is its frontmatter
`title:`, else its first H1, else its filename. Hidden, .reflectignore'd
(folder-name patterns), and raw/ folders and symlinks are outside Reflect's
index, except the archive tier's link into raw_store, which it lists read-only;
secure/ notes, which Reflect shows read-only, are off-limits here. None is ever
opened.

`nonnative()` names the syntax reflect-open's renderer shows as raw text,
ignores, or folds, so audits and migrations share one definition.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

from _paths import tier_segments  # type: ignore[import-not-found]

STORE_FOLDERS = {"raw", "secure"}
SETTINGS = Path.home() / "Library/Application Support/reflect-open/settings.json"
# Characters Reflect forbids inside [[...]]; `#` starts a fragment.
FORBIDDEN_RE = re.compile(r"[\[\]|\\#\n]")
WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\[\]|\n]+?)(?:\|([^\[\]\n]*))?\]\]")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_H1_RE = re.compile(r"^ {0,3}# +(\S.*?)(?:\s+#+)?\s*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIERS = ("date", "title", "alias", "title~", "alias~", "stem")


@dataclass(frozen=True)
class Note:
    path: Path  # relative to the vault root
    title: str | None  # authored title; None means the filename stands in
    aliases: tuple[str, ...]


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def read_head(path: Path) -> tuple[str | None, tuple[str, ...]]:
    """Frontmatter `title:` and `aliases:`, else the first ATX H1 outside fences."""
    title: str | None = None
    aliases: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        first = handle.readline()
        rest: Iterator[str] = handle
        if first.rstrip() == "---":
            block: list[str] = []
            for line in handle:
                if line.rstrip() == "---":
                    break
                block.append(line)
            else:  # unterminated: not frontmatter
                rest, block = iter([first, *block]), []
            in_aliases = False
            for line in block:
                if in_aliases and (m := re.match(r"^\s+-\s+(.*)$", line)):
                    aliases.append(_scalar(m.group(1)))
                    continue
                in_aliases = False
                if line.startswith("title:") and title is None:
                    title = _scalar(line[6:]) or None
                elif line.startswith("aliases:"):
                    value = line[8:].strip()
                    if value.startswith("[") and value.endswith("]"):
                        aliases += [_scalar(a) for a in value[1:-1].split(",")]
                    elif value:
                        aliases.append(_scalar(value))
                    else:
                        in_aliases = True
        else:
            rest = itertools.chain([first], handle)
        if title is None:
            fenced = False
            for line in rest:
                if _FENCE_RE.match(line):
                    fenced = not fenced
                elif not fenced and (m := _H1_RE.match(line)):
                    title = m.group(1)
                    break
    return title, tuple(a for a in aliases if a)


def fold_key(title: str) -> str:
    """Reflect's foldKey, the exact match: NFC, trimmed, lowercase."""
    return unicodedata.normalize("NFC", title).strip().lower()


def title_key(title: str) -> str:
    """Approximate Reflect's foldFallbackTitleKey: the fold key with collapsed
    whitespace and leading emoji (non-ASCII symbols, joiners, selectors) dropped."""
    key = " ".join(fold_key(title).split())
    while key and ((unicodedata.category(key[0]) in ("So", "Sk") and ord(key[0]) > 0x2000) or key[0] in "‍️"):
        key = key[1:]
    return key.lstrip()


def walk(root: Path, skip: set[str]) -> Iterator[tuple[Path, list[str], list[str]]]:
    """`os.walk` over visible folders not named in `skip`, entering only the archive tier's link."""
    archive = root / tier_segments().get("archive", "archive")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in skip
                             and (here / d == archive or not (here / d).is_symlink()))
        yield here, dirnames, filenames


def backup_limit(root: Path) -> int | None:
    """reflect-open's per-graph Git backup size guard in bytes, if this graph sets one."""
    try:
        entries = json.loads(SETTINGS.read_text(encoding="utf-8")).get("backupMaxFileMiB", {})
    except (OSError, ValueError, AttributeError):
        return None
    mib = entries.get(str(root), entries.get(str(root.resolve()))) if isinstance(entries, dict) else None
    return mib * 1024 * 1024 if type(mib) is int and 1 <= mib <= 95 else None


def notes(root: Path) -> Iterator[Note]:
    """Every Reflect-visible note under `root`, in path order."""
    ignore = root / ".reflectignore"
    patterns = ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else []
    ignored = {p.strip().strip("/") for p in patterns if p.strip() and not p.lstrip().startswith("#")}
    for here, _dirnames, filenames in walk(root, ignored | STORE_FOLDERS):
        for name in sorted(filenames):
            path = here / name
            if name.endswith(".md") and not path.is_symlink():
                title, aliases = read_head(path)
                yield Note(path.relative_to(root), title, aliases)


class TitleIndex:
    """Which note a `[[X]]` names, the way Reflect resolves it."""

    def __init__(self, root: Path):
        self.root = root
        daily = Path(tier_segments().get("daily_notes", "daily"))
        self._claims: dict[str, dict[str, set[Path]]] = {t: defaultdict(set) for t in TIERS}
        self._titles: dict[Path, str] = {}
        for note in notes(root):
            title = note.title or note.path.stem
            self._titles[note.path] = title
            if note.path.parent == daily and _DATE_RE.match(note.path.stem):
                self._claims["date"][note.path.stem].add(note.path)
            for key, fold in (("title", fold_key), ("title~", title_key)):
                self._claims[key][fold(title)].add(note.path)
                for alias in note.aliases:
                    self._claims[key.replace("title", "alias")][fold(alias)].add(note.path)
            self._claims["stem"][fold_key(note.path.stem)].add(note.path)

    @staticmethod
    def _key(tier: str, target: str) -> str:
        return title_key(target) if tier.endswith("~") else fold_key(target)

    def title(self, path: Path) -> str | None:
        """The title Reflect shows for `path` (vault-relative), if it indexes it."""
        return self._titles.get(path)

    def resolve(self, target: str) -> Path | None:
        """The one note `[[target]]` opens, or None when missing or ambiguous."""
        for tier in TIERS:
            if paths := self._claims[tier].get(self._key(tier, target)):
                return next(iter(paths)) if len(paths) == 1 else None
        return None

    def has(self, target: str) -> bool:
        """Does `[[target]]` name any note, even ambiguously?"""
        return any(self._key(tier, target) in self._claims[tier] for tier in TIERS)

    def link_title(self, path: Path) -> str | None:
        """The title that links `path` (vault-relative) unambiguously, if any."""
        title = self.title(path)
        if title is None or FORBIDDEN_RE.search(title) or self.resolve(title) != path:
            return None
        return title


# Line syntax reflect-open's renderer (meowdown) shows as raw text, ignores, or folds.
# Reflect tasks are only `+ [ ]`/`+ [x]`; own-line and `<!-- {json} -->` comments are hidden.
_LINE_KINDS = {
    "block_id": re.compile(r"(?<!\S)\^[A-Za-z0-9][A-Za-z0-9-]*(?=\s*(?:\||$))"),
    "task_marker": re.compile(r"^\s*[-*+] \[[~/]\]"),
    "plus_bullet": re.compile(r"^\s*\+ (?!\[[ xX~/]\])"),
    "html": re.compile(
        r"<(?:/?(?:a|b|blockquote|br|center|code|details|div|em|font|h[1-6]|hr|i|iframe|img|kbd|li|mark|ol|p"
        r"|pre|small|span|strong|sub|summary|sup|table|td|th|tr|u|ul|video)\b[^>\n]*>|!--(?!\s*\{))",
        re.I,
    ),
    "heading_link": re.compile(r"(?<!!)\[[^\]\n]*\]\(<?#"),
    "angle_dest": re.compile(r"\]\(<"),
    "callout": re.compile(r"^\s*>\s*\[![A-Za-z]+\]"),
    "footnote": re.compile(r"\[\^[^\]\s]+\]"),
    "obsidian_comment": re.compile(r"%%[^%\n]+%%|^\s*%%\s*$"),
    "dataview": re.compile(r"^\s*[A-Za-z][\w-]*::\s"),
    "entity": re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9A-Fa-f]+);"),
}
_CODE_SPAN_RE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
_NOTE_LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\((<[^>\n]+>|[^()\s]+)\)")
_BLOCK_START_RE = re.compile(
    r"^ {0,3}(?:[-*+]\s|\d+[.)]\s|#|>|\||<|!\[|\$\$|```|~~~|(?:-{2,}|=+|\*{3,}|_{3,})\s*$)"
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_RULE_RE = re.compile(r"^-{3,}\s*$")
_YAML_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*:(?:\s|$)")
_TERMINAL = tuple(".!?。！？:;：；")


def _scan(lines: list[str]) -> Iterator[tuple[int, str]]:
    """(index, line) for body lines outside frontmatter, fences, `$$` math, and comment blocks."""
    start = 0
    if lines and lines[0].rstrip() == "---":
        start = next((i + 1 for i, line in enumerate(lines[1:], 1) if line.rstrip() == "---"), 0)
    fence = math = comment = False
    for i in range(start, len(lines)):
        line, stripped = lines[i], lines[i].strip()
        if comment:
            comment = "-->" not in line
        elif _FENCE_RE.match(line):
            fence = not fence
        elif not fence and stripped == "$$":
            math = not math
        elif not fence and not math and stripped.startswith("<!--"):
            comment = "-->" not in stripped
        elif not fence and not math:
            yield i, line


def _is_text(line: str) -> bool:
    return bool(line.strip()) and not _BLOCK_START_RE.match(line)


def _cjk(char: str) -> bool:
    return unicodedata.east_asian_width(char) in "WF"


def _mid_sentence(prev: str, nxt: str, strict: bool) -> bool:
    """Does the break between `prev` and `nxt` fall inside running prose?

    `strict` (a note that also has long unwrapped lines, so short breaks may be
    intended) accepts only a lowercase continuation or a break between two CJK letters.
    """
    head, tail = nxt.lstrip(), prev.rstrip().rstrip("*_\"')]）」』】》”’")
    if not head or not tail or prev.rstrip().endswith(("**", "__")):
        return False  # a bold line reads as a heading of what follows
    if strict:
        return ((head[0].isascii() and head[0].islower() and not tail.endswith(_TERMINAL))
                or all(_cjk(c) and unicodedata.category(c).startswith("L") for c in (tail[-1], head[0])))
    if not (head[0].isalnum() or head[0] in "\"“‘'([`"):
        return False  # a marker such as `@cite:`, `→`, `_(`, or an emoji starts its own line
    if re.match(r"\d+(?:\.\d+)*[.)]\s|[A-Z]{1,4}\b", head) or (
            tail.endswith(_TERMINAL) and (head[0].isupper() or _cjk(head[0]))):
        return False  # numbering, an `OR`/`TODO` word, or a new sentence on a new line
    return True


def wrapped_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """[start, end) line ranges of hard-wrapped paragraphs or list items.

    Reflect shows every newline as a break, so a block counts as wrapped only
    when each line but the last is 55-100 characters, none ends in a hard
    break, most end mid-sentence, and every break falls inside running prose,
    judged strictly unless the note's prose is wrapped throughout.
    """
    body = dict(_scan(lines))
    prose = [line for line in body.values() if _is_text(line) or _LIST_ITEM_RE.match(line)]
    strict = sum(len(line) > 110 for line in prose) > max(1, len(prose) // 20)
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if i not in body or not (_is_text(lines[i]) or _LIST_ITEM_RE.match(lines[i])) or lines[i].startswith("    "):
            i += 1
            continue
        j = i + 1
        while j in body and _is_text(lines[j]):
            j += 1
        head = lines[i:j - 1]
        if (j - i >= 2 and all(55 <= len(line.rstrip()) <= 100 for line in head)
                and not any(line.endswith(("  ", "\\")) for line in head)
                and 2 * sum(not line.rstrip().endswith(_TERMINAL) for line in head) >= len(head)
                and all(_mid_sentence(lines[k], lines[k + 1], strict) for k in range(i, j - 1))):
            out.append((i, j))
        i = j
    return out


def join_wrapped(lines: list[str], start: int, end: int) -> str:
    """One line from a wrapped block; CJK neighbours join without a space."""
    text = lines[start].rstrip()
    for line in lines[start + 1:end]:
        part = line.strip()
        cjk = unicodedata.east_asian_width(text[-1:] or " ") in "WF" and unicodedata.east_asian_width(part[:1] or " ") in "WF"
        text += ("" if cjk else " ") + part
    return text


def note_link_target(dest: str, note: Path, titles: TitleIndex) -> Path | None:
    """The Reflect note (vault-relative) a Markdown link destination opens, if any."""
    path = unquote(dest.strip().removeprefix("<").removesuffix(">").partition("#")[0])
    if not path.lower().endswith(".md") or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", path):
        return None
    target = Path(os.path.normpath(note.parent / path))
    if not target.is_relative_to(titles.root):
        return None
    rel = target.relative_to(titles.root)
    return rel if titles.title(rel) is not None or ("secure" in rel.parts and target.is_file()) else None


def nonnative(text: str, note: Path | None = None, titles: TitleIndex | None = None) -> Counter:
    """Counts of syntax in a note body that reflect-open does not render natively.

    With the note's absolute path and a TitleIndex, a relative `.md` link to a
    Reflect note (secure ones included) counts as `md_note_link`: it opens, but
    never backlinks or follows renames.
    """
    lines = text.splitlines()
    hits: Counter = Counter()
    if lines and lines[0].rstrip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip() == "---"), 0)
        hits["frontmatter_tags"] += any(line.startswith("tags:") for line in lines[1:end])
    body = dict(_scan(lines))
    for i, line in body.items():
        bare = _CODE_SPAN_RE.sub("", line)
        for kind, pattern in _LINE_KINDS.items():
            hits[kind] += len(pattern.findall(bare))
        if _RULE_RE.match(line):  # a mid-note YAML block shows as a rule, text, and a heading
            j = i + 1
            while _YAML_KEY_RE.match(body.get(j, "")):
                j += 1
            hits["yaml_block"] += j > i + 1 and bool(_RULE_RE.match(body.get(j, "")))
        if note is not None and titles is not None:
            hits["md_note_link"] += sum(
                note_link_target(dest, note, titles) is not None for dest in _NOTE_LINK_RE.findall(bare)
            )
    hits["hard_wrap"] = len(wrapped_blocks(lines))
    hits["multiline_wikilink"] = len(re.findall(r"\[\[[^\[\]\n]*\n[^\[\]]*?\]\]", text))
    return +hits
