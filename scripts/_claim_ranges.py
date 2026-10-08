"""Markdown claim ranges and citation occurrences in unchanged source coordinates.

Internal spans are half-open Python code-point offsets. ``byte_range`` is the
UTF-8 interchange boundary; Markdown's normalized text never becomes the source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_inline.html_inline import html_inline

CLAIM_RE = re.compile(r"<!--[ \t]*(/?)claim:c([1-9][0-9]*)[ \t]*-->")
RESERVED_RE = re.compile(r"<!--\s*/?claim\b", re.I)
REFERENCE_RE = re.compile(r"\[\[([^\[\]\n|]*)\|ref\]\](?:(<!--(?!\s*/?claim\b)[^\n]*?-->))?")
WIKILINK_RE = re.compile(r"\[\[[^\[\]\n]*\]\]")
LEGACY_CITE_RE = re.compile(r"[ \t]*@cite:[^\n]*")
LEDGER_HEADINGS = {"Evidence", "References"}


@dataclass(frozen=True, slots=True)
class ClaimRange:
    number: int
    start: int
    end: int


def byte_range(text: str, start: int, end: int) -> list[int]:
    return [len(text[:start].encode("utf-8")), len(text[:end].encode("utf-8"))]


def _parser() -> MarkdownIt:
    def legacy_cite(state, silent):
        if silent or state.linkLevel or state.pos and state.src[state.pos - 1] != "\n":
            return False
        match = LEGACY_CITE_RE.match(state.src, state.pos)
        if match is None:
            return False
        token = state.push("legacy_cite", "", 0)
        token.content = match[0]
        token.meta = {"local_range": match.span()}
        state.pos = match.end()
        return True

    def comment(state, silent):
        start, linked = state.pos, state.linkLevel
        if not html_inline(state, silent):
            if RESERVED_RE.match(state.src, start) is None:
                return False
            # An incomplete reserved comment is an error, not an implicit range
            # through the remaining document. Escapes/code never enter this rule.
            end = state.src.find("\n", start)
            state.pos = len(state.src) if end < 0 else end
            if not silent:
                state.push("html_inline", "", 0).content = state.src[start:state.pos]
        if not silent:
            state.tokens[-1].meta = {"local_range": (start, state.pos), "linked": bool(linked)}
        return True

    def reference(state, silent):
        match = REFERENCE_RE.match(state.src, state.pos)
        kind = "wiki_reference" if match is not None else "wiki_link"
        match = match or WIKILINK_RE.match(state.src, state.pos)
        if (match is None or state.linkLevel or silent
                or re.search(r"(?<!\\)(?:\\\\)*!$", state.src[:state.pos])):
            return False
        token = state.push(kind, "", 0)
        token.content = match[0]
        token.meta = {"match": match, "local_range": match.span()}
        state.pos = match.end()
        return True

    parser = MarkdownIt("commonmark")
    parser.inline.ruler.before("text", "legacy_cite", legacy_cite)
    parser.inline.ruler.at("html_inline", comment)
    parser.inline.ruler.after("link", "wiki_reference", reference)
    return parser


def _signature(tokens):
    """Rendered structure without range comments, positions, or text-token splits."""
    result = []
    for token in tokens:
        if token.type in {"html_inline", "html_block"} and CLAIM_RE.fullmatch(token.content.strip()):
            continue
        if token.type == "text":
            if not token.content:
                continue
            if result and result[-1][0] == "text":
                result[-1] = ("text", result[-1][1] + token.content)
            else:
                result.append(("text", token.content))
        else:
            result.append((token.type, token.tag, token.nesting, token.hidden,
                           tuple(token.attrs.items()),
                           _signature(token.children) if token.children is not None else token.content))
    return result


class MarkdownSource:
    def __init__(self, text: str):
        self.text = text
        self.errors: list[str] = []
        self.ranges: list[ClaimRange] = []
        self.markers: list[tuple[int, int, bool, int]] = []
        self.references = []
        self.legacy_cites = []
        self.has_claim_syntax = False
        # CommonMark normalizes CRLF/CR and NUL before parsing. Keep its boundary map.
        chars, origins = [], []
        pos = 0
        while pos < len(text):
            origins.append(pos)
            char = text[pos]
            chars.append("\n" if char == "\r" else "\ufffd" if char == "\0" else char)
            pos += 2 if text[pos:pos + 2] == "\r\n" else 1
        origins.append(len(text))
        normalized = "".join(chars)
        starts = [0, *(m.end() for m in re.finditer("\n", normalized))]

        def at_line(line):
            return starts[line] if line < len(starts) else len(normalized)

        def inline_offset(block, offset):
            row = block.content.count("\n", 0, offset)
            column = offset - (block.content.rfind("\n", 0, offset) + 1)
            content = block.content.split("\n")[row]
            start = at_line(block.map[0] + row)
            raw = normalized[start:at_line(block.map[0] + row + 1)].rstrip("\n")
            indent = raw.find(content)
            if indent < 0:
                raise ValueError("unsupported Markdown source mapping")
            return origins[start + indent + column]

        parser = _parser()
        self.tokens = parser.parse(normalized)
        for index, block in enumerate(self.tokens):
            if block.map is not None:
                block.meta["source_range"] = tuple(origins[at_line(i)] for i in block.map)
            if block.type == "html_block" and RESERVED_RE.match(block.content.lstrip()):
                start, end = block.meta["source_range"]
                raw = block.content.strip()
                if CLAIM_RE.fullmatch(raw):
                    start = text.index(raw, start, end)
                    self._marker(raw, start, start + len(raw))
                else:
                    self.has_claim_syntax = True
                    self.error(start, "a marker cannot begin a line it shares with prose, as in a list item or quote")
            for token in block.children or []:
                if "local_range" not in token.meta or token.type not in {"wiki_reference", "wiki_link", "legacy_cite"} and not RESERVED_RE.match(token.content):
                    continue
                try:
                    start, end = (inline_offset(block, i) for i in token.meta["local_range"])
                except ValueError as exc:
                    self.error(block.meta["source_range"][0], str(exc))
                    continue
                token.meta["source_range"] = (start, end)
                if token.type in {"wiki_reference", "wiki_link", "legacy_cite"}:
                    if re.search(r"<!--\s*/?claim\b", token.content, re.I):
                        self.error(start, "claim boundary bisects a citation")
                    if token.type == "wiki_reference":
                        self.references.append(token)
                    elif token.type == "legacy_cite" and not text[text.rfind("\n", 0, start) + 1:start].strip():
                        self.legacy_cites.append(token)
                elif RESERVED_RE.match(token.content):
                    self._marker(token.content, start, end)
                    if token.meta["linked"] or index == 0 or self.tokens[index - 1].type != "paragraph_open":
                        self.error(start, "claim endpoint is not a prose position")
        self._pair()
        for table in MarkdownIt("commonmark").enable("table").parse(normalized if "|" in text else ""):
            a, b = (origins[at_line(i)] for i in table.map) if table.type == "table_open" else (0, 0)
            for r in [r for r in self.ranges if (a <= r.start < b or a < r.end <= b)
                      and re.search(r"(?<!\\)\||\n", WIKILINK_RE.sub("", text[r.start:r.end]))]:
                self.error(r.start, f"c{r.number} crosses a table cell")
                self.ranges.remove(r)
        if self.markers and not self.errors:
            stripped = text
            for start, end, _closing, _number in reversed(self.markers):
                stripped = stripped[:start] + stripped[end:]
            if _signature(self.tokens) != _signature(parser.parse(stripped)):
                self.error(self.markers[0][0], "claim markers change Markdown text or formatting")
                self.ranges.clear()

    def error(self, offset: int, message: str):
        self.errors.append(f"line {self.text.count(chr(10), 0, offset) + 1}: {message}")

    def _marker(self, raw: str, start: int, end: int):
        self.has_claim_syntax = True
        match = CLAIM_RE.fullmatch(raw)
        if match is None:
            self.error(start, "malformed reserved claim marker")
        elif not self.text[self.text.rfind("\n", 0, start) + 1:start].strip() and not re.match(
                r"\s*\Z|[ \t]*\r?\n[ \t]*\r?\n", self.text[end:]):
            self.error(start, "a line-leading claim marker needs a blank line before the next prose")
        else:
            self.markers.append((start, end, bool(match[1]), int(match[2])))

    def _pair(self):
        self.markers.sort()
        groups = {}
        for marker in self.markers:
            groups.setdefault(marker[3], []).append(marker)
        pairs = []
        for number, markers in groups.items():
            if len(markers) != 2 or [m[2] for m in markers] != [False, True]:
                self.error(markers[0][0], f"c{number} requires one opening and one later closing marker")
                continue
            opening, closing = markers
            if not self.text[opening[1]:closing[0]].strip():
                self.error(opening[0], f"c{number} has an empty range")
                continue
            pairs.append((opening, closing))
        administrative = []
        section = None
        for j, block in enumerate(self.tokens):
            if block.type == "heading_open" and block.tag == "h2":
                if section is not None:
                    administrative.append((section, block.meta["source_range"][0]))
                section = (block.meta["source_range"][0]
                           if self.tokens[j + 1].content in LEDGER_HEADINGS | {"Revision Log"} else None)
            elif block.type == "fence" and block.info.split()[:1] == ["anchors"]:
                administrative.append(block.meta["source_range"])
        if section is not None:
            administrative.append((section, len(self.text)))
        invalid = set()
        for i, (opening, closing) in enumerate(pairs):
            for other_open, other_close in pairs[:i]:
                if opening[0] < other_close[1] and other_open[0] < closing[1]:
                    self.error(opening[0], "nested or crossing claim ranges")
                    invalid.update((opening[3], other_open[3]))
            for start, end in administrative:
                if opening[1] < end and start < closing[0]:
                    self.error(opening[0], "claim range encloses evidence or revision records")
                    invalid.add(opening[3])
        self.ranges = [ClaimRange(a[3], a[1], b[0]) for a, b in pairs if a[3] not in invalid]
