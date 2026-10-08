#!/usr/bin/env python3
"""Compute deterministic TrustRank with dated evidence and reviewer floors.

Anchors seed PageRank; citations propagate it; passes add no graph mass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from markdown_it import MarkdownIt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier, vault_root  # type: ignore[import-not-found]  # noqa: E402
from _claim_ranges import LEDGER_HEADINGS, MarkdownSource, REFERENCE_RE, byte_range  # noqa: E402
from _reflect import TitleIndex, read_head  # noqa: E402

WIKI_DIR = tier("wiki")
DAMPING = 0.85
FLOOR = 0.1
MAX_ITER = 200
TOL = 1e-9

ANCHOR_TYPES = {"s2", "arxiv", "doi", "isbn", "url", "gist"}
PASS_AGENTS = {"reviewer", "challenger", "thinker", "scout", "curator", "editor"}
PASS_STATUSES = {"verified", "flagged", "inconclusive", "pending"}

FENCE_OPEN_RE = re.compile(r"^```anchors\s*$")
FENCE_CLOSE_RE = re.compile(r"^```\s*$")
# Unified format: [[Note Title#^c3]] or [[Note Title]] (note-level)
CITE_TARGET_RE = re.compile(r"^\[\[([^\]#]+?)(?:#\^c([1-9][0-9]*))?\]\]\s*$")
BARE_CITE_RE = re.compile(r"^\s*@cite:\s+")


# Data model


@dataclass(slots=True, eq=False, repr=False)
class Marker:
    kind: str
    fields: dict
    line_no: int
    raw: str

    @property
    def valid_at(self) -> date | None:
        v = self.fields.get("valid_at")
        return _parse_iso(v) if v else None

    @property
    def invalid_at(self) -> date | None:
        v = self.fields.get("invalid_at")
        return _parse_iso(v) if v else None

    def active_on(self, as_of: date) -> bool:
        # Evidence validity is half-open: [valid_at, invalid_at).
        va = self.valid_at
        if va is None or va > as_of:
            return False
        ia = self.invalid_at
        if ia is not None and ia <= as_of:
            return False
        return True


class Claim:
    def __init__(self, note_path: Path, number: int, title: str, line_no: int):
        self.note_path = note_path
        self.number = number
        self.title = title
        self.line_no = line_no
        self.body_lines: list[str] = []
        self.anchors: list[Marker] = []
        self.cites: list[Marker] = []
        self.passes: list[Marker] = []
        self.source_range: tuple[int, int] = (0, 0)
        self.range_utf8: list[int] | None = None

    @property
    def key(self) -> str:
        return f"{self.note_path.as_posix()}#C{self.number}"

    def has_body(self) -> bool:
        body = REFERENCE_RE.sub("", "\n".join(self.body_lines))
        body = re.sub(r"(?<!!)\[ref\]\[[^\]\n]+\]", "", body)
        return any(char.isalnum() for block in MarkdownIt("commonmark").parse(body)
                   for token in block.children or [] if token.type in {"text", "code_inline"}
                   for char in token.content)


class WikiNote:
    def __init__(self, path: Path):
        self.path = path
        self.title: str | None = None
        self.claims: list[Claim] = []
        self.provenance: list[Marker] = []
        self.parse_errors: list[str] = []

    def integrity_ok(self) -> bool:
        return not self.parse_errors

    def has_reviewer_pass(self, as_of: date) -> bool:
        for claim in self.claims:
            for p in claim.passes:
                if not p.active_on(as_of):
                    continue
                if (
                    p.fields.get("_agent") == "reviewer"
                    and p.fields.get("status") == "verified"
                ):
                    return True
        return False


# Parser

def _parse_iso(value: str) -> date | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _split_marker_line(line: str) -> tuple[str, str, list[str]] | None:
    """Split recognized markers into kind, first value and extras; other lines return None."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    for kind in ("@anchor", "@cite", "@pass"):
        prefix = kind + ":"
        if stripped.startswith(prefix):
            rest = stripped[len(prefix):]
            parts = [p.strip() for p in rest.split(" | ")]
            if not parts:
                return None
            return kind, parts[0], parts[1:]
    return None


def _parse_marker(kind: str, first: str, extras: list[str], line_no: int, raw: str) -> tuple[Marker | None, str | None]:
    fields: dict[str, str] = {}

    if kind == "@anchor":
        atype, sep, aid = first.partition(":")
        if not sep or not atype or not aid:
            return None, f"line {line_no}: malformed @anchor value `{first}`"
        if atype not in ANCHOR_TYPES:
            return None, f"line {line_no}: unrecognized @anchor type `{atype}`"
        fields["_anchor_type"] = atype
        fields["_anchor_id"] = aid
        fields["_node_id"] = f"{atype}:{aid}"
    elif kind == "@cite":
        m = CITE_TARGET_RE.match(first)
        if not m:
            return None, f"line {line_no}: malformed @cite target `{first}`"
        fields["_cite_title"] = m.group(1).strip()
        fields["_cite_claim_number"] = m.group(2) or ""
    elif kind == "@pass":
        agent = first.strip()
        if agent not in PASS_AGENTS:
            return None, f"line {line_no}: unrecognized @pass agent `{agent}`"
        fields["_agent"] = agent

    for extra in extras:
        if ":" not in extra:
            return None, f"line {line_no}: malformed field `{extra}`"
        k, _, v = extra.partition(":")
        fields[k.strip()] = v.strip()

    # Normalize pass `at:` into the shared validity field.
    if kind == "@pass" and "valid_at" not in fields and "at" in fields:
        fields["valid_at"] = fields["at"]

    va = _parse_iso(fields.get("valid_at", ""))
    if va is None:
        return None, f"line {line_no}: missing or invalid valid_at"
    # --as-of filters evidence; it never permits markers dated after wall-clock today.
    if va > date.today():
        return None, f"line {line_no}: valid_at `{fields['valid_at']}` is in the future (item 9)"
    if "invalid_at" in fields:
        ia = _parse_iso(fields["invalid_at"])
        if ia is None or ia <= va:
            return None, f"line {line_no}: invalid_at must be > valid_at"

    if kind == "@pass":
        status = fields.get("status", "")
        if status not in PASS_STATUSES:
            return None, f"line {line_no}: unrecognized @pass status `{status}`"
        if (fields["_agent"] == "editor") != (status == "pending"):
            return None, f"line {line_no}: editor and pending are reserved for the editor/pending pair"
        if fields["_agent"] == "editor" and fields.get("at") != fields["valid_at"]:
            return None, f"line {line_no}: editor/pending requires an ISO at date matching valid_at"

    return Marker(kind, fields, line_no, raw), None


def _citation_markers(document: MarkdownSource):
    for token in document.references:
        start, end = token.meta["source_range"]
        match = token.meta["match"]
        line = document.text.count("\n", 0, start) + 1
        try:
            fields = json.loads((match[2] or "")[4:-3])["metadata"]["citation"]
            if (not isinstance(fields, dict) or set(fields) - {"valid_at", "invalid_at"}
                    or not all(isinstance(v, str) for v in fields.values())):
                raise ValueError("invalid citation fields")
        except (ValueError, TypeError, KeyError):
            yield start, end, None, f"line {line}: missing or invalid citation metadata"
            continue
        marker, err = _parse_marker("@cite", f"[[{match[1]}]]",
                                    [f"{k}: {v}" for k, v in fields.items()], line, match[0])
        yield start, end, marker, err


def _record_marker(note: WikiNote, claim: Claim | None, parsed, line: int, raw: str) -> None:
    kind, first, extras = parsed
    marker, err = _parse_marker(kind, first, extras, line, raw)
    if err:
        note.parse_errors.append(err)
    elif claim is None:
        note.parse_errors.append(f"line {line}: marker outside any claim body")
    else:
        getattr(claim, {"@anchor": "anchors", "@cite": "cites", "@pass": "passes"}[kind]).append(marker)


def parse_wiki_note(path: Path, today: date) -> WikiNote:
    note = WikiNote(path)
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as e:
        note.parse_errors.append(f"read error: {e}")
        return note
    document = MarkdownSource(text)
    note.parse_errors.extend(document.errors)
    legacy_sections: list[tuple[int, int]] = []
    section_start: int | None = None
    current_claim: Claim | None = None
    for i, block in enumerate(document.tokens):
        if block.type != "heading_open":
            continue
        start, end = block.meta["source_range"]
        title = document.tokens[i + 1].content
        if block.tag == "h1" and note.title is None:
            note.title = title.strip()
        if block.tag == "h2":
            if section_start is not None:
                legacy_sections.append((section_start, start))
            if current_claim is not None:
                current_claim.source_range = (current_claim.source_range[0], start)
            section_start = end if title == "Claims" else None
            current_claim = None
        match = re.fullmatch(r"\[C([1-9][0-9]*)\]\s*(.*)", title) if block.tag == "h3" else None
        if section_start is not None and block.tag == "h3" and title.startswith("[C") and match is None:
            note.parse_errors.append(f"line {block.map[0] + 1}: malformed legacy claim heading")
        if section_start is not None and match:
            if current_claim is not None:
                current_claim.source_range = (current_claim.source_range[0], start)
            current_claim = Claim(path, int(match[1]), match[2], block.map[0] + 1)
            current_claim.source_range = (end, len(text))
            note.claims.append(current_claim)
    if section_start is not None:
        legacy_sections.append((section_start, len(text)))
    for item in document.ranges:
        body = text[item.start:item.end]
        title = next((line.strip() for line in body.splitlines() if line.strip()), "")
        claim = Claim(path, item.number, REFERENCE_RE.sub("", title).strip(), text.count("\n", 0, item.start) + 1)
        claim.source_range = (item.start, item.end)
        claim.range_utf8 = byte_range(text, item.start, item.end)
        note.claims.append(claim)
    note.claims.sort(key=lambda claim: claim.source_range)
    owners = {}
    for index, claim in enumerate(note.claims):
        if claim.number in owners:
            note.parse_errors.append(f"duplicate or mixed legacy/article claim c{claim.number}")
        if any(c.source_range[1] > claim.source_range[0] for c in note.claims[:index]):
            note.parse_errors.append(f"overlapping legacy/article claim ownership at c{claim.number}")
        owners[claim.number] = claim
        body = text[slice(*claim.source_range)]
        for token in reversed(document.legacy_cites):
            start, end = token.meta["source_range"]
            if claim.source_range[0] <= start and end <= claim.source_range[1]:
                body = body[:start - claim.source_range[0]] + body[end - claim.source_range[0]:]
        claim.body_lines = body.splitlines()

    def owner(start, end):
        matches = [c for c in note.claims if c.source_range[0] <= start and end <= c.source_range[1]]
        if len(matches) > 1:
            note.parse_errors.append(f"line {text.count(chr(10), 0, start) + 1}: overlapping claim ownership")
        return matches[0] if len(matches) == 1 else None

    fences = set()
    section = ""
    for i, block in enumerate(document.tokens):
        if block.type == "heading_open" and block.tag == "h2":
            section = document.tokens[i + 1].content
        if block.type == "fence" and block.info.split()[:1] == ["anchors"]:
            start, end = block.meta["source_range"]
            match = re.fullmatch(r"anchors(?: (c[1-9][0-9]*))?", block.info.strip())
            if match is None:
                note.parse_errors.append(f"line {block.map[0] + 1}: malformed anchors fence owner")
                continue
            claim = owners.get(int(match[1][1:])) if match[1] else owner(start, end)
            if match[1]:
                if match[1] in fences or claim is None or claim.range_utf8 is None or section not in LEDGER_HEADINGS:
                    note.parse_errors.append(f"line {block.map[0] + 1}: duplicate, orphan, or misplaced anchors owner {match[1]}")
                fences.add(match[1])
            elif claim is None:
                note.parse_errors.append(f"line {block.map[0] + 1}: anchors fence outside any claim body")
            elif claim is not None and claim.range_utf8 is not None:
                note.parse_errors.append(f"line {block.map[0] + 1}: article evidence requires an owned anchors fence")
            last = text[start:end].splitlines()[-1]
            if not re.fullmatch(r" {0,3}" + re.escape(block.markup[0]) + "{" + str(len(block.markup)) + r",}\s*", last):
                note.parse_errors.append(f"line {block.map[0] + 1}: unclosed anchors fence")
            for offset, raw in enumerate(block.content.splitlines(), start=block.map[0] + 2):
                parsed = _split_marker_line(raw)
                if parsed is not None:
                    _record_marker(note, claim, parsed, offset, raw)
                elif raw.strip() and not raw.strip().startswith("#"):
                    note.parse_errors.append(f"line {offset}: non-marker line inside anchors fence: `{raw.strip()}`")
    for token in document.legacy_cites:
        start, end = token.meta["source_range"]
        _record_marker(note, owner(start, end), _split_marker_line(token.content),
                       text.count("\n", 0, start) + 1, token.content)
    for start, end, marker, err in _citation_markers(document):
        claim = owner(start, end)
        crossing = any(a < end and start < b and not (a <= start and end <= b)
                       for a, b in (c.source_range for c in note.claims))
        if err or crossing:
            note.parse_errors.append(err or f"line {marker.line_no}: citation crosses a claim boundary")
        elif claim is not None:
            claim.cites.append(marker)
        elif any(a <= start < b for a, b in legacy_sections):
            note.parse_errors.append(f"line {marker.line_no}: reference outside any claim body")
        else:
            note.provenance.append(marker)
    if note.title is None:
        note.parse_errors.append("missing H1 title")
    else:
        note.title = read_head(path)[0] or note.title
    if not note.claims:
        note.parse_errors.append("no claims found" if document.has_claim_syntax or legacy_sections else "missing claim ranges or `## Claims` section")
    for claim in note.claims:
        if not claim.has_body():
            note.parse_errors.append(
                f"[C{claim.number}] has no body text"
            )
    return note


# Graph construction + PageRank


def _resolve_cites(notes: list[WikiNote]) -> None:
    """Resolve before graph construction; ordinary notes provide provenance only."""
    wiki = {note.path: note for note in notes}
    visible = None
    ordinary = {}
    for note in notes:
        for c in [*note.provenance, *(c for claim in note.claims for c in claim.cites)]:
            c.fields.pop("_provenance_only", None)
            target_title = c.fields["_cite_title"]
            if visible is None:
                visible = TitleIndex(vault_root(), skip_unreadable=True)
            relative = visible.resolve(target_title)
            target_path = visible.root / relative if relative is not None else None
            if target_path is None:
                note.parse_errors.append(f"line {c.line_no}: @cite target `[[{target_title}]]` not found or ambiguous")
                continue
            c.fields["_cite_path"] = target_path.as_posix()
            target_note = wiki.get(target_path)
            target_cn = c.fields.get("_cite_claim_number") or ""
            if target_note is None:
                c.fields["_provenance_only"] = "true"
                if target_cn and target_path not in ordinary:
                    ordinary[target_path] = parse_wiki_note(target_path, date.today())
                target_note = ordinary.get(target_path)
            if target_cn and (target_note is None or not any(str(claim.number) == target_cn for claim in target_note.claims)
                              or c.fields.get("_provenance_only") and any(e != "missing H1 title" for e in target_note.parse_errors)):
                note.parse_errors.append(f"line {c.line_no}: @cite target `[[{target_title}]] #C{target_cn}` does not exist or has invalid ownership")


def build_graph(
    notes: list[WikiNote], as_of: date
) -> tuple[list[str], dict[str, list[str]], dict[str, float], set[str]]:
    """Build anchor -> claim and cited -> citing edges after citation validation.

    Only valid notes participate; active anchors receive uniform personalization.
    """
    notes_by_path: dict[Path, WikiNote] = {n.path: n for n in notes}

    anchor_nodes: set[str] = set()
    claim_nodes: set[str] = set()
    edges: list[tuple[str, str]] = []

    for note in notes:
        if not note.integrity_ok():
            # A note that fails structural integrity contributes neither
            # seed mass nor propagation edges. Its claims still become
            # nodes so they appear in the report with score 0.
            for claim in note.claims:
                claim_nodes.add(claim.key)
            continue

        for claim in note.claims:
            claim_nodes.add(claim.key)

            for a in claim.anchors:
                if not a.active_on(as_of):
                    continue
                node_id = a.fields["_node_id"]
                anchor_nodes.add(node_id)
                edges.append((node_id, claim.key))

            for c in claim.cites:
                if not c.active_on(as_of) or c.fields.get("_provenance_only"):
                    continue
                target_path = Path(c.fields["_cite_path"])  # Resolved by _resolve_cites.
                target_note = notes_by_path[target_path]
                # A cite edge only propagates trust from a source note that
                # itself passed integrity. If the target failed pass 1, we
                # silently drop the edge (the target's score is zero anyway).
                if not target_note.integrity_ok():
                    continue
                target_cn = c.fields.get("_cite_claim_number") or ""
                if target_cn:
                    target_key = f"{target_path.as_posix()}#C{target_cn}"
                    edges.append((target_key, claim.key))
                else:
                    # No specific claim: edge from every claim in the
                    # target note to this claim, each with equal share
                    # via PageRank's natural split by out-degree.
                    for other in target_note.claims:
                        edges.append((other.key, claim.key))

    nodes = sorted(anchor_nodes | claim_nodes)
    out_edges: dict[str, list[str]] = {v: [] for v in nodes}
    for src, dst in edges:
        out_edges[src].append(dst)

    personalization: dict[str, float] = {}
    if anchor_nodes:
        share = 1.0 / len(anchor_nodes)
        for a in anchor_nodes:
            personalization[a] = share

    return nodes, out_edges, personalization, claim_nodes


def pagerank(
    nodes: list[str],
    out_edges: dict[str, list[str]],
    personalization: dict[str, float],
    alpha: float = DAMPING,
    max_iter: int = MAX_ITER,
    tol: float = TOL,
) -> dict[str, float]:
    """Iterate personalized PageRank; dangling and teleport mass follow personalization."""
    n = len(nodes)
    if n == 0:
        return {}
    idx = {v: i for i, v in enumerate(nodes)}

    total_p = sum(personalization.values())
    if total_p <= 0:
        # No seeds means zero trust; uniform personalization would invent intrinsic trust.
        return {v: 0.0 for v in nodes}
    p = [personalization.get(v, 0.0) / total_p for v in nodes]

    r = [1.0 / n] * n
    dangling = [1 if not out_edges.get(v) else 0 for v in nodes]

    for _ in range(max_iter):
        dangling_mass = alpha * sum(r[i] for i in range(n) if dangling[i])
        r_new = [(1.0 - alpha) * p[i] + dangling_mass * p[i] for i in range(n)]
        for v in nodes:
            targets = out_edges.get(v) or []
            if not targets:
                continue
            i = idx[v]
            share = alpha * r[i] / len(targets)
            for t in targets:
                r_new[idx[t]] += share
        delta = sum(abs(r_new[i] - r[i]) for i in range(n))
        r = r_new
        if delta < tol:
            break

    return {nodes[i]: r[i] for i in range(n)}


# Scoring


def score_notes(
    notes: list[WikiNote], as_of: date
) -> tuple[dict[str, float], dict[Path, float]]:
    """Return (claim_scores, note_scores) after PageRank + floor trust."""
    _resolve_cites(notes)
    nodes, out_edges, personalization, claim_nodes = build_graph(notes, as_of)
    raw = pagerank(nodes, out_edges, personalization)

    claim_scores: dict[str, float] = {
        k: raw.get(k, 0.0) for k in claim_nodes
    }

    # Apply claim-level floor trust.
    for note in notes:
        if not note.integrity_ok():
            continue
        if not note.has_reviewer_pass(as_of):
            continue
        for claim in note.claims:
            if claim_scores.get(claim.key, 0.0) < FLOOR:
                claim_scores[claim.key] = FLOOR

    note_scores: dict[Path, float] = {}
    for note in notes:
        if not note.claims:
            note_scores[note.path] = 0.0
            continue
        vals = [claim_scores.get(c.key, 0.0) for c in note.claims]
        note_scores[note.path] = sum(vals) / len(vals)

    return claim_scores, note_scores


# CLI


def load_wiki(as_of: date, only: Path | None = None) -> list[WikiNote]:
    if only is not None:
        if not only.exists():
            sys.stderr.write(f"trust.py: no such file: {only}\n")
            sys.exit(2)
        # Resolve relative --note paths before comparing against the absolute wiki root.
        only_abs = only.resolve()
        if WIKI_DIR not in only_abs.parents or "secure" in only_abs.relative_to(WIKI_DIR).parts:
            sys.stderr.write(
                f"trust.py: {only} is not under {WIKI_DIR} (structural integrity item 1)\n"
            )
            sys.exit(2)
        return [parse_wiki_note(only_abs, as_of)]

    if not WIKI_DIR.exists():
        sys.stderr.write(f"trust.py: {WIKI_DIR} does not exist\n")
        sys.exit(2)

    # Exclude auto-generated files that don't follow wiki schema.
    excluded = {"index.md"}

    notes: list[WikiNote] = []
    # Recurse into domain buckets; exclusions also apply to their index basenames.
    for path in sorted(WIKI_DIR.rglob("*.md")):
        if path.name in excluded or "secure" in path.relative_to(WIKI_DIR).parts or path.is_symlink():
            continue
        notes.append(parse_wiki_note(path, as_of))
    return notes


def format_table(
    notes: list[WikiNote],
    claim_scores: dict[str, float],
    note_scores: dict[Path, float],
    as_of: date,
) -> str:
    lines = []
    lines.append(f"TrustRank report  (as-of {as_of.isoformat()})")
    lines.append(f"Scanned {len(notes)} wiki entries under {WIKI_DIR}/")
    lines.append("")
    header = f"{'score':>7}  {'claims':>6}  {'status':<10}  note"
    lines.append(header)
    lines.append("-" * len(header))

    ranked = sorted(
        notes,
        key=lambda n: (-note_scores.get(n.path, 0.0), n.path.as_posix()),
    )
    for note in ranked:
        score = note_scores.get(note.path, 0.0)
        status = "ok" if note.integrity_ok() else "fail"
        lines.append(
            f"{score:7.4f}  {len(note.claims):6d}  {status:<10}  {note.path.as_posix()}"
        )

    broken = [n for n in notes if not n.integrity_ok()]
    if broken:
        lines.append("")
        lines.append("Structural integrity failures:")
        for n in broken:
            lines.append(f"  {n.path.as_posix()}")
            for err in n.parse_errors:
                lines.append(f"    - {err}")
    return "\n".join(lines) + "\n"


def format_note_detail(
    note: WikiNote,
    claim_scores: dict[str, float],
    note_scores: dict[Path, float],
    as_of: date,
) -> str:
    lines = []
    lines.append(f"TrustRank note detail  (as-of {as_of.isoformat()})")
    lines.append(f"Note: {note.path.as_posix()}")
    lines.append(f"Title: {note.title or '(missing)'}")
    status = "ok" if note.integrity_ok() else "FAIL"
    lines.append(f"Structural integrity: {status}")
    lines.append(f"Note score (mean of claims): {note_scores.get(note.path, 0.0):.4f}")
    lines.append("")
    if note.parse_errors:
        lines.append("Parse errors:")
        for err in note.parse_errors:
            lines.append(f"  - {err}")
        lines.append("")
    lines.append(f"{'claim':<6}  {'score':>7}  {'anchors':>7}  {'cites':>5}  {'passes':>6}  title")
    lines.append("-" * 70)
    for claim in note.claims:
        n_anchors = sum(1 for a in claim.anchors if a.active_on(as_of))
        n_cites = sum(1 for c in claim.cites if c.active_on(as_of))
        n_passes = sum(1 for p in claim.passes if p.active_on(as_of))
        score = claim_scores.get(claim.key, 0.0)
        title = claim.title
        if len(title) > 60:
            title = title[:57] + "..."
        lines.append(
            f"[C{claim.number}]".ljust(6)
            + f"  {score:7.4f}  {n_anchors:7d}  {n_cites:5d}  {n_passes:6d}  {title}"
        )
    return "\n".join(lines) + "\n"


def format_json(
    notes: list[WikiNote],
    claim_scores: dict[str, float],
    note_scores: dict[Path, float],
    as_of: date,
) -> str:
    payload = {
        "as_of": as_of.isoformat(),
        "wiki_dir": WIKI_DIR.as_posix(),
        "damping": DAMPING,
        "floor": FLOOR,
        "notes": [],
    }
    for note in sorted(notes, key=lambda n: n.path.as_posix()):
        payload["notes"].append(
            {
                "path": note.path.as_posix(),
                "title": note.title,
                "integrity_ok": note.integrity_ok(),
                "parse_errors": list(note.parse_errors),
                "note_score": round(note_scores.get(note.path, 0.0), 6),
                "claims": [
                    {
                        "number": c.number,
                        "title": c.title,
                        "score": round(claim_scores.get(c.key, 0.0), 6),
                        "anchors": sum(1 for a in c.anchors if a.active_on(as_of)),
                        "cites": sum(1 for ct in c.cites if ct.active_on(as_of)),
                        "passes": sum(1 for p in c.passes if p.active_on(as_of)),
                        **({"range_utf8": c.range_utf8} if c.range_utf8 is not None else {}),
                    }
                    for c in note.claims
                ],
            }
        )
    return json.dumps(payload, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/trust.py",
        description="TrustRank over the configured wiki (deterministic Personalized PageRank).",
    )
    parser.add_argument(
        "--note",
        type=Path,
        default=None,
        help="Show per-claim breakdown for a single wiki entry.",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Bi-temporal snapshot date (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON for /lint consumption.",
    )
    args = parser.parse_args(argv)

    if args.as_of:
        as_of = _parse_iso(args.as_of)
        if as_of is None:
            sys.stderr.write(f"trust.py: invalid --as-of date `{args.as_of}`\n")
            return 2
    else:
        as_of = date.today()

    target = None
    if args.note is not None:
        # Score the full corpus so --note can resolve its citation targets.
        candidate = load_wiki(as_of, only=args.note)[0]
        notes = load_wiki(as_of)
        target = next(
            (n for n in notes if n.path.resolve() == candidate.path.resolve()),
            None,
        )
        if target is None:
            # Not in the corpus walk (e.g. an excluded basename): fall back
            # to single-note scoring.
            notes = [candidate]
            target = candidate
    else:
        notes = load_wiki(as_of)
    claim_scores, note_scores = score_notes(notes, as_of)

    if args.json:
        json_notes = [target] if target is not None else notes
        sys.stdout.write(format_json(json_notes, claim_scores, note_scores, as_of))
        return 0

    if target is not None:
        sys.stdout.write(format_note_detail(target, claim_scores, note_scores, as_of))
        return 0

    sys.stdout.write(format_table(notes, claim_scores, note_scores, as_of))
    return 0


if __name__ == "__main__":
    sys.exit(main())
