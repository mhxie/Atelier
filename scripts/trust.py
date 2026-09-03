#!/usr/bin/env python3
"""
trust.py: TrustRank for the zk/wiki/ knowledge layer.

Walks zk/wiki/*.md, parses each wiki entry into claims + anchor/cite/pass
markers, builds a directed trust graph, and runs Personalized PageRank with
the teleport distribution restricted to external @anchor seed nodes. The
result is a per-claim trust score; note-level scores are the mean of their
claims.

Algorithm (Gyongyi, Garcia-Molina, Pedersen, VLDB 2004):

    Seed:      external @anchor markers become seed nodes. Their initial
               mass is the PageRank personalization vector. Non-anchored
               claims start at zero.
    Propagate: internal @cite markers become directed edges from the cited
               claim to the citing claim. Trust flows along cite edges.
    Floor:     internal @pass markers never accumulate trust. A wiki entry
               that passes structural integrity (items 1 to 10 of
               protocols/wiki-schema.md) AND has at least one
               `@pass: reviewer | status: verified` earns a claim-level
               floor of 0.1 on every claim in the note.

Personalized PageRank is the full implementation. The random-walk teleport
distribution puts all mass on anchor seed nodes; all other nodes contribute
no initial mass. Damping factor 0.85.

This implementation is deterministic and stdlib-only. It avoids the
networkx dependency by doing a direct power-iteration PageRank with
dangling-node mass redistributed to the personalization vector, matching
`networkx.pagerank(G, personalization=anchor_dict)` semantics.

Bi-temporal: every marker carries valid_at, optional invalid_at. The
`--as-of YYYY-MM-DD` flag filters markers by the temporal window. Default
as-of is today.

CLI:
    scripts/trust.py                         default table over zk/wiki/
    scripts/trust.py --note zk/wiki/foo.md   per-claim breakdown
    scripts/trust.py --as-of 2025-06-01      bi-temporal snapshot
    scripts/trust.py --json                  structured output for /lint
    scripts/trust.py --index                 write zk/wiki/index.md

Paths are project-relative. Run from the repo root.

See also:
    protocols/wiki-schema.md            the schema this parser enforces
    protocols/local-first-architecture.md  layer model
    handoffs/<date>-<topic>.md           optional local design handoff
        (gitignored and not shipped in clones)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import parse_iso_date, tier  # type: ignore[import-not-found]  # noqa: E402

WIKI_DIR = tier("wiki")
DAMPING = 0.85
FLOOR = 0.1
MAX_ITER = 200
TOL = 1e-9

ANCHOR_TYPES = {"s2", "arxiv", "doi", "isbn", "url", "gist"}
PASS_AGENTS = {"reviewer", "challenger", "thinker", "scout", "curator"}
PASS_STATUSES = {"verified", "flagged", "inconclusive"}

CLAIM_HEADING_RE = re.compile(r"^###\s+\[C(\d+)\]\s*(.*)$")
CLAIMS_HEADING_RE = re.compile(r"^##\s+Claims\s*$")
FENCE_OPEN_RE = re.compile(r"^```anchors\s*$")
FENCE_CLOSE_RE = re.compile(r"^```\s*$")
H1_RE = re.compile(r"^#\s+(.+?)\s*$")
# Unified format: [[Note Title#^c3]] or [[Note Title]] (note-level)
CITE_TARGET_RE = re.compile(r"^\[\[([^\]#]+?)(?:#\^c(\d+))?\]\]\s*$")
BARE_CITE_RE = re.compile(r"^\s*@cite:\s+")


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------


class Marker:
    __slots__ = (
        "kind",
        "fields",
        "line_no",
        "raw",
    )

    def __init__(self, kind: str, fields: dict, line_no: int, raw: str):
        self.kind = kind
        self.fields = fields
        self.line_no = line_no
        self.raw = raw

    @property
    def valid_at(self) -> date | None:
        v = self.fields.get("valid_at")
        return _parse_iso(v) if v else None

    @property
    def invalid_at(self) -> date | None:
        v = self.fields.get("invalid_at")
        return _parse_iso(v) if v else None

    def active_on(self, as_of: date) -> bool:
        # Window: [valid_at, invalid_at). invalid_at is an EXCLUSIVE
        # boundary: a marker becomes dead at 00:00 on its invalid_at
        # date. This matches the schema's "markers are never deleted,
        # only invalidated" story: if you invalidate on 2026-04-12,
        # queries as-of 2026-04-12 see the marker as already dead.
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

    @property
    def key(self) -> str:
        return f"{self.note_path.as_posix()}#C{self.number}"

    def has_body(self) -> bool:
        return any(line.strip() for line in self.body_lines)


class WikiNote:
    def __init__(self, path: Path):
        self.path = path
        self.title: str | None = None
        self.claims: list[Claim] = []
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


# ----------------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------------


def _parse_iso(s: str) -> date | None:
    return parse_iso_date(s)


def _split_marker_line(line: str) -> tuple[str, str, list[str]] | None:
    """Split `@kind: first | k: v | k: v` into (kind, first_value, extras).

    Returns None if the line is blank, a comment, or does not begin with a
    recognized marker prefix.
    """
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

    # `@pass` markers use `at:` as their temporal anchor (schema line 108).
    # `@anchor` and `@cite` use `valid_at:`. Normalize @pass into `valid_at`
    # so the bi-temporal filter has a single field to check.
    if kind == "@pass" and "valid_at" not in fields and "at" in fields:
        fields["valid_at"] = fields["at"]

    va = _parse_iso(fields.get("valid_at", ""))
    if va is None:
        return None, f"line {line_no}: missing or invalid valid_at"
    # Schema § Structural Integrity item 9: valid_at must be <= today (wall
    # clock). The --as-of flag is a separate bi-temporal *filter*, not a
    # relaxation of item 9: the point of item 9 is that markers cannot be
    # backdated from the future. `today` is the real current date.
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

    return Marker(kind, fields, line_no, raw), None


def parse_wiki_note(path: Path, today: date) -> WikiNote:
    note = WikiNote(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        note.parse_errors.append(f"read error: {e}")
        return note

    lines = text.splitlines()

    # Item 1 is enforced by the caller (walking WIKI_DIR). Items 2-10 here.

    in_claims_section = False
    in_fence = False
    current_claim: Claim | None = None

    for idx, raw in enumerate(lines, start=1):
        if note.title is None:
            m = H1_RE.match(raw)
            if m:
                note.title = m.group(1).strip()
                continue

        if CLAIMS_HEADING_RE.match(raw):
            in_claims_section = True
            continue

        # A level-2 heading other than `## Claims` closes the claims section.
        if in_claims_section and raw.startswith("## ") and not CLAIMS_HEADING_RE.match(raw):
            if in_fence:
                note.parse_errors.append(
                    f"line {idx}: `{raw.strip()}` appeared while an anchors fence was still open (item 5)"
                )
            in_claims_section = False
            current_claim = None
            in_fence = False
            continue

        if not in_claims_section:
            continue

        if in_fence:
            if FENCE_CLOSE_RE.match(raw):
                in_fence = False
                continue
            parsed = _split_marker_line(raw)
            if parsed is None:
                if raw.strip() and not raw.strip().startswith("#"):
                    note.parse_errors.append(
                        f"line {idx}: non-marker line inside anchors fence: `{raw.strip()}`"
                    )
                continue
            kind, first, extras = parsed
            marker, err = _parse_marker(kind, first, extras, idx, raw)
            if err:
                note.parse_errors.append(err)
                continue
            if current_claim is None:
                note.parse_errors.append(
                    f"line {idx}: marker outside any claim body"
                )
                continue
            if marker.kind == "@anchor":
                current_claim.anchors.append(marker)
            elif marker.kind == "@cite":
                current_claim.cites.append(marker)
            else:
                current_claim.passes.append(marker)
            continue

        if FENCE_OPEN_RE.match(raw):
            in_fence = True
            continue

        # Stray fence close with no open.
        if FENCE_CLOSE_RE.match(raw):
            continue

        # Bare @cite outside fence (Obsidian backlink format).
        if BARE_CITE_RE.match(raw):
            if current_claim is None:
                note.parse_errors.append(
                    f"line {idx}: @cite marker outside any claim body"
                )
                continue
            parsed = _split_marker_line(raw)
            if parsed is not None:
                kind, first, extras = parsed
                if kind == "@cite":
                    marker, err = _parse_marker(kind, first, extras, idx, raw)
                    if err:
                        note.parse_errors.append(err)
                    else:
                        current_claim.cites.append(marker)
                    continue
                else:
                    note.parse_errors.append(
                        f"line {idx}: `{kind}` marker must be inside a fenced anchors block"
                    )
                    continue

        m = CLAIM_HEADING_RE.match(raw)
        if m:
            number = int(m.group(1))
            title = m.group(2).strip()
            expected = len(note.claims) + 1
            if number != expected:
                note.parse_errors.append(
                    f"line {idx}: claim number [C{number}] is not sequential (expected [C{expected}])"
                )
            current_claim = Claim(path, number, title, idx)
            note.claims.append(current_claim)
            continue

        if current_claim is not None:
            current_claim.body_lines.append(raw)

    # Post-parse structural checks.
    if note.title is None:
        note.parse_errors.append("missing H1 title")
    if not any(CLAIMS_HEADING_RE.match(line) for line in lines):
        note.parse_errors.append("missing `## Claims` section")
    if not note.claims and in_claims_section is False and not any(note.parse_errors):
        # Empty claims section is itself a structural fail.
        note.parse_errors.append("no claims found under `## Claims`")
    for claim in note.claims:
        if not claim.has_body():
            note.parse_errors.append(
                f"[C{claim.number}] has no body text"
            )
    if in_fence:
        note.parse_errors.append("unclosed anchors fence at end of file")

    return note


# ----------------------------------------------------------------------------
# Graph construction + PageRank
# ----------------------------------------------------------------------------


def _resolve_cites(notes: list[WikiNote]) -> None:
    """Pass 1: validate every @cite target and append any dangling-cite
    errors to the owning note's parse_errors list. This must run BEFORE
    edge construction so that a note which turns out to have a broken
    cite never contributes seeds or edges on the strength of its other
    valid markers. Codex P1 fix: dangling-cite errors used to be appended
    inside the same loop that built edges, so edges from a note with any
    valid markers survived even after the note was marked failed.
    """
    title_index: dict[str, Path] = {}
    for note in notes:
        if note.title:
            title_index[note.title] = note.path

    claim_keys: set[str] = set()
    for note in notes:
        for claim in note.claims:
            claim_keys.add(claim.key)

    for note in notes:
        for claim in note.claims:
            for c in claim.cites:
                target_title = c.fields["_cite_title"]
                target_path = title_index.get(target_title)
                if target_path is None:
                    note.parse_errors.append(
                        f"line {c.line_no}: @cite target `[[{target_title}]]` not found in zk/wiki/"
                    )
                    continue
                target_cn = c.fields.get("_cite_claim_number") or ""
                if target_cn:
                    target_key = f"{target_path.as_posix()}#C{target_cn}"
                    if target_key not in claim_keys:
                        note.parse_errors.append(
                            f"line {c.line_no}: @cite target `[[{target_title}]] #C{target_cn}` does not exist"
                        )


def build_graph(
    notes: list[WikiNote], as_of: date
) -> tuple[list[str], dict[str, list[str]], dict[str, float], set[str]]:
    """Return (nodes, out_edges, personalization, claim_nodes).

    Edge direction: cited -> citing. Anchor -> claim.
    Personalization is uniform over anchor seed nodes active on `as_of`.

    Two-pass construction:
      Pass 1 (done by the caller via `_resolve_cites`): validate all
             @cite targets, append dangling-cite errors.
      Pass 2 (this function): build the trust graph using only notes
             that pass structural integrity AFTER pass 1.
    """
    title_index: dict[str, Path] = {}
    for note in notes:
        if note.title:
            title_index[note.title] = note.path

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
                if not c.active_on(as_of):
                    continue
                target_title = c.fields["_cite_title"]
                target_path = title_index[target_title]  # Resolved in pass 1.
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
    """Personalized PageRank via power iteration.

    Matches networkx.pagerank(G, personalization=...) semantics:
      - Dangling nodes (no out-edges) redistribute their mass to the
        personalization vector.
      - Teleport mass (1 - alpha) goes to the personalization vector.
      - If personalization is empty, falls back to uniform.
    """
    n = len(nodes)
    if n == 0:
        return {}
    idx = {v: i for i, v in enumerate(nodes)}

    total_p = sum(personalization.values())
    if total_p <= 0:
        # No trust seeds. TrustRank with an empty seed set is zero
        # everywhere: no mass enters the graph. Do not fall back to
        # uniform personalization here — uniform would imply that every
        # claim is intrinsically trusted, which is the opposite of the
        # seed-only semantics in Gyongyi et al. 2004.
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


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------


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


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def load_wiki(as_of: date, only: Path | None = None) -> list[WikiNote]:
    if only is not None:
        if not only.exists():
            sys.stderr.write(f"trust.py: no such file: {only}\n")
            sys.exit(2)
        # WIKI_DIR is absolute ($OV/wiki); resolve `only` to absolute too,
        # otherwise the documented relative form (--note zk/wiki/foo.md
        # via the project's zk symlink) would be rejected because
        # `only.parents` would compare relative-to-absolute.
        only_abs = only.resolve()
        if WIKI_DIR not in only_abs.parents:
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
    # rglob (recursive): wiki entries live in domain subdirectories
    # (one directory per domain cluster), not at the top level. A non-recursive
    # glob silently scanned 0 entries after the corpus was reorganized into
    # subdirs. `excluded` matches by basename, so per-subdir index.md files
    # are skipped too.
    for path in sorted(WIKI_DIR.rglob("*.md")):
        if path.name in excluded:
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
                    }
                    for c in note.claims
                ],
            }
        )
    return json.dumps(payload, indent=2) + "\n"


REVISION_DATE_RE = re.compile(r"^-\s+\**(\d{4}-\d{2}-\d{2})")


def _last_revised(path: Path) -> str:
    """Extract the most recent date from the ## Revision Log section."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    in_revision = False
    for line in text.splitlines():
        if re.match(r"^##\s+Revision Log\s*$", line):
            in_revision = True
            continue
        if in_revision and line.startswith("## "):
            break
        if in_revision:
            m = REVISION_DATE_RE.match(line)
            if m:
                return m.group(1)  # Latest-first per schema.
    return "unknown"


def format_index(
    notes: list[WikiNote],
    claim_scores: dict[str, float],
    note_scores: dict[Path, float],
    as_of: date,
) -> str:
    """Generate a markdown index of the wiki corpus with trust scores."""
    lines: list[str] = []
    lines.append("# Wiki Index")
    lines.append("")
    lines.append(f"Auto-generated by `scripts/trust.py --index` on {as_of.isoformat()}.")
    lines.append(f"Scanned **{len(notes)}** entries under `{WIKI_DIR}/`.")
    lines.append("")

    # Build cite-edge summary for the graph section.
    title_to_path: dict[str, Path] = {}
    for n in notes:
        if n.title is not None:
            title_to_path[n.title] = n.path

    outbound: dict[Path, list[str]] = {n.path: [] for n in notes}
    inbound: dict[Path, list[str]] = {n.path: [] for n in notes}
    for note in notes:
        if not note.integrity_ok():
            continue
        for claim in note.claims:
            for c in claim.cites:
                target_title = c.fields.get("_cite_title", "")
                target_path = title_to_path.get(target_title)
                if target_path and target_path != note.path and note.title:
                    if target_title not in outbound[note.path]:
                        outbound[note.path].append(target_title)
                    if note.title not in inbound[target_path]:
                        inbound[target_path].append(note.title)

    # Entries table, ranked by trust score descending.
    ranked = sorted(
        notes,
        key=lambda n: (-note_scores.get(n.path, 0.0), n.path.as_posix()),
    )

    lines.append("## Entries")
    lines.append("")
    for note in ranked:
        score = note_scores.get(note.path, 0.0)
        n_claims = len(note.claims)
        n_anchors = sum(
            1 for cl in note.claims for a in cl.anchors if a.active_on(as_of)
        )
        revised = _last_revised(note.path)
        status = "ok" if note.integrity_ok() else "FAIL"
        title = note.title or "(untitled)"
        slug = note.path.stem

        lines.append(f"### [[{slug}|{title}]]")
        lines.append("")
        lines.append(f"- **Score:** {score:.4f}  |  **Claims:** {n_claims}  |  **Anchors:** {n_anchors}  |  **Status:** {status}")
        lines.append(f"- **Last revised:** {revised}")

        cited_by = inbound.get(note.path, [])
        cites_out = outbound.get(note.path, [])
        if cited_by:
            lines.append(f"- **Cited by:** {', '.join(f'[[{t}]]' for t in cited_by)}")
        if cites_out:
            lines.append(f"- **Cites:** {', '.join(f'[[{t}]]' for t in cites_out)}")
        if not cited_by and not cites_out:
            lines.append("- **Graph:** isolated (no @cite edges)")

        lines.append("")

    # Graph summary.
    total_cites = sum(len(v) for v in outbound.values())
    isolated = sum(1 for n in notes if not inbound[n.path] and not outbound[n.path])
    lines.append("## Graph Summary")
    lines.append("")
    lines.append(f"- **Total @cite edges:** {total_cites}")
    lines.append(f"- **Isolated entries:** {isolated} / {len(notes)}")
    lines.append(f"- **Mean trust score:** {sum(note_scores.values()) / max(len(note_scores), 1):.4f}")
    lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/trust.py",
        description="TrustRank over zk/wiki/ (Personalized PageRank, deterministic, stdlib-only).",
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
    parser.add_argument(
        "--index",
        action="store_true",
        help="Generate zk/wiki/index.md with trust-scored corpus overview.",
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
        # Validate + resolve the requested note, but score against the FULL
        # corpus: a single-note corpus cannot resolve @cite targets to other
        # entries, which falsely failed structural integrity (and floored the
        # scores) for any healthy note citing another.
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

    if args.index:
        # --index writes zk/wiki/index.md (not stdout).
        index_path = WIKI_DIR / "index.md"
        # --index needs the full corpus, not a single note.
        if args.note is not None:
            sys.stderr.write("trust.py: --index cannot be combined with --note\n")
            return 2
        all_notes = load_wiki(as_of, only=None)
        all_claim_scores, all_note_scores = score_notes(all_notes, as_of)
        content = format_index(all_notes, all_claim_scores, all_note_scores, as_of)
        index_path.write_text(content, encoding="utf-8")
        sys.stderr.write(f"trust.py: wrote {index_path.as_posix()}\n")
        return 0

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
