#!/usr/bin/env python3
"""Check wiki corpus structure; only ERROR findings fail the command."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

# trust.py lives next to this file and is importable as a library.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _findings  # noqa: E402
from _findings import Finding  # noqa: E402
from _paths import wiki_dirs  # type: ignore[import-not-found]  # noqa: E402
from trust import (  # noqa: E402
    BARE_CITE_RE,
    FENCE_CLOSE_RE,
    FENCE_OPEN_RE,
    REFERENCE_RE,
    WIKI_DIR,
    WikiNote,
    _resolve_cites,
    load_wiki,
)

VOCABULARY_PATH = Path(__file__).resolve().parent / "wiki_vocabulary.txt"


def localized_shadow_dirs() -> list[Path]:
    """Return registry-backed localized wikis, excluding the primary wiki."""
    return wiki_dirs()[1:]

SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}

# Path to optional file listing URL prefixes to skip in readwise-missing check.
# One prefix per line. Intended for private repo URLs where git is the evidence.
READWISE_SKIP_FILE = Path(__file__).resolve().parent / "readwise_skip_domains.txt"

# --- Unfounded-term detection regexes ---
# ALL-CAPS acronyms (2+ chars), e.g. SIMD, MVCC, OCC
ACRONYM_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,})\b")
# CamelCase words, e.g. PyArrow, RecordBatch, DataLoader
CAMELCASE_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]*)+)\b")
# Backtick-wrapped terms, e.g. `take_rows()`, `RecordBatch`
BACKTICK_RE = re.compile(r"`([^`]+)`")




def check_parse_errors(notes: list[WikiNote]) -> list[Finding]:
    findings: list[Finding] = []
    for note in notes:
        for err in note.parse_errors:
            findings.append(
                Finding(
                    "ERROR",
                    "parse-error",
                    note.path.as_posix(),
                    err,
                )
            )
    return findings


def check_duplicate_titles(notes: list[WikiNote]) -> list[Finding]:
    findings: list[Finding] = []
    by_title: dict[str, list[Path]] = {}
    for note in notes:
        if not note.title:
            continue
        by_title.setdefault(note.title, []).append(note.path)
    for title, paths in by_title.items():
        if len(paths) > 1:
            for p in paths:
                others = [q.as_posix() for q in paths if q != p]
                findings.append(
                    Finding(
                        "ERROR",
                        "duplicate-title",
                        p.as_posix(),
                        f"title `{title}` also used by: {', '.join(others)} (breaks @cite target resolution)",
                    )
                )
    return findings


def check_slug_alignment(notes: list[WikiNote]) -> list[Finding]:
    findings: list[Finding] = []
    for note in notes:
        if not note.title:
            continue
        expected = note.title
        actual = note.path.stem
        if actual != expected:
            findings.append(
                Finding(
                    "WARN",
                    "slug-mismatch",
                    note.path.as_posix(),
                    f"filename stem `{actual}` does not match title `{expected}` — "
                    f"rename the file or adjust the H1 so @cite target resolution stays stable",
                )
            )
    return findings


def check_graph_topology(notes: list[WikiNote]) -> list[Finding]:
    """Find orphan entries, unciting entries and unlinked entries sharing anchor sources."""
    findings: list[Finding] = []
    ok_notes = [n for n in notes if n.integrity_ok() and n.title]

    if len(ok_notes) < 2:
        return findings

    title_to_path: dict[str, Path] = {}
    for n in ok_notes:
        if n.title is not None:
            title_to_path[n.title] = n.path
    inbound: dict[Path, set[Path]] = {n.path: set() for n in ok_notes}
    outbound: dict[Path, set[Path]] = {n.path: set() for n in ok_notes}

    for note in ok_notes:
        for claim in note.claims:
            for c in claim.cites:
                if c.fields.get("_provenance_only"):
                    continue
                target_title = c.fields.get("_cite_title", "")
                target_path = Path(c.fields["_cite_path"]) if "_cite_path" in c.fields else title_to_path.get(target_title)
                if target_path in inbound and target_path != note.path:
                    outbound[note.path].add(target_path)
                    inbound[target_path].add(note.path)

    for note in ok_notes:
        if not inbound[note.path]:
            findings.append(
                Finding(
                    "WARN",
                    "orphan-entry",
                    note.path.as_posix(),
                    f"no other wiki entry cites `{note.title}` — "
                    f"consider inline citation references inside supported claims of related entries",
                )
            )

    for note in ok_notes:
        if not outbound[note.path]:
            findings.append(
                Finding(
                    "INFO",
                    "no-outbound-cite",
                    note.path.as_posix(),
                    f"`{note.title}` does not cite any other wiki entry",
                )
            )

    anchor_to_notes: dict[str, set[Path]] = {}
    for note in ok_notes:
        for claim in note.claims:
            for a in claim.anchors:
                node_id = a.fields.get("_node_id", "")
                if node_id:
                    anchor_to_notes.setdefault(node_id, set()).add(note.path)

    reported_pairs: set[tuple[str, str]] = set()
    for anchor_id, paths in anchor_to_notes.items():
        if len(paths) < 2:
            continue
        sorted_paths = sorted(paths, key=lambda p: p.as_posix())
        for i, pa in enumerate(sorted_paths):
            for pb in sorted_paths[i + 1:]:
                pair_key = (pa.as_posix(), pb.as_posix())
                if pair_key in reported_pairs:
                    continue
                if pb in outbound[pa] or pa in outbound[pb]:
                    continue
                reported_pairs.add(pair_key)
                title_a = next(n.title for n in ok_notes if n.path == pa)
                title_b = next(n.title for n in ok_notes if n.path == pb)
                findings.append(
                    Finding(
                        "INFO",
                        "shared-anchor-no-cite",
                        f"{pa.as_posix()} + {pb.as_posix()}",
                        f"`{title_a}` and `{title_b}` share @anchor `{anchor_id}` "
                        f"but have no citation link; consider an inline reference where one claim supports another",
                    )
                )

    return findings


def load_vocabulary() -> set[str]:
    """Load lowercase allowed terms; a missing vocabulary returns an empty set."""
    if not VOCABULARY_PATH.exists():
        return set()
    terms: set[str] = set()
    for line in VOCABULARY_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        terms.add(stripped.lower())
    return terms


def _strip_anchors_and_cites(lines: list[str]) -> list[str]:
    """Exclude structured evidence from prose scanned for jargon."""
    result: list[str] = []
    in_fence = False
    for line in lines:
        if FENCE_OPEN_RE.match(line):
            in_fence = True
            continue
        if in_fence:
            if FENCE_CLOSE_RE.match(line):
                in_fence = False
            continue
        if BARE_CITE_RE.match(line):
            continue
        result.append(REFERENCE_RE.sub("", line))
    return result


def _has_inline_explanation(text: str, term: str, window: int = 80) -> bool:
    """Look for a parenthetical definition within window characters after term."""
    idx = 0
    term_lower = term.lower()
    text_lower = text.lower()
    while True:
        pos = text_lower.find(term_lower, idx)
        if pos == -1:
            return False
        after = text[pos + len(term): pos + len(term) + window]
        # Look for " (" pattern near the term
        paren_pos = after.find("(")
        if paren_pos != -1 and paren_pos < 40:
            return True
        idx = pos + 1


def check_unfounded_terms(notes: list[WikiNote]) -> list[Finding]:
    """Informational readability check for jargon lacking a vocabulary entry, wiki title or definition."""
    findings: list[Finding] = []
    vocab = load_vocabulary()
    if not vocab:
        findings.append(
            Finding(
                "INFO",
                "vocabulary-missing",
                VOCABULARY_PATH.as_posix() if VOCABULARY_PATH.exists() else "scripts/wiki_vocabulary.txt",
                "vocabulary allowlist not found or empty; unfounded-term check skipped",
            )
        )
        return findings

    wiki_titles_lower: set[str] = set()
    for note in notes:
        if note.title:
            wiki_titles_lower.add(note.title.lower())

    ok_notes = [n for n in notes if n.integrity_ok() and n.title]

    for note in ok_notes:
        prose_lines: list[str] = []
        for claim in note.claims:
            prose_lines.extend(_strip_anchors_and_cites(claim.body_lines))
            prose_lines.append(claim.title)

        prose_text = "\n".join(prose_lines)

        candidates: dict[str, str] = {}  # lowered -> original form

        for m in ACRONYM_RE.finditer(prose_text):
            raw = m.group(1)
            candidates.setdefault(raw.lower(), raw)

        for m in CAMELCASE_RE.finditer(prose_text):
            raw = m.group(1)
            candidates.setdefault(raw.lower(), raw)

        # 3. Backtick-wrapped terms: only flag CamelCase class/type names.
        #    Backtick formatting already signals "this is code" to the reader,
        #    so snake_case identifiers, function calls, config keys, etc. are
        #    self-grounding. CamelCase terms in backticks are the exception:
        #    they name concepts (classes, protocols) that may need explanation.
        for m in BACKTICK_RE.finditer(prose_text):
            raw = m.group(1).strip()
            name = re.sub(r"\(.*\)$", "", raw)
            if not CAMELCASE_RE.match(name):
                if "." in name:
                    parts = name.split(".")
                    final = parts[-1]
                    if CAMELCASE_RE.match(final):
                        candidates.setdefault(final.lower(), final)
                continue
            candidates.setdefault(name.lower(), name)

        unfounded: list[str] = []
        for term_lower, term_orig in sorted(candidates.items()):
            if term_lower in vocab:
                continue

            if any(term_lower in wt for wt in wiki_titles_lower):
                continue

            if any(wt in term_lower for wt in wiki_titles_lower):
                continue

            if _has_inline_explanation(prose_text, term_orig):
                continue

            if term_orig.startswith("@"):
                continue

            if term_orig.startswith("If-"):
                continue

            if len(term_orig) <= 2:
                continue

            unfounded.append(term_orig)

        if unfounded:
            term_list = ", ".join(sorted(unfounded))
            findings.append(
                Finding(
                    "INFO",
                    "unfounded-term",
                    note.path.as_posix(),
                    f"{len(unfounded)} term(s) not in vocabulary allowlist "
                    f"and not matching any wiki entry: {term_list}. "
                    f"Consider adding a wiki entry, an inline explanation, "
                    f"or adding to scripts/wiki_vocabulary.txt if common knowledge.",
                )
            )

    return findings


def check_readwise_backfill(notes: list[WikiNote]) -> list[Finding]:
    """Warn on URL/gist anchors lacking the recommended Readwise fallback."""
    skip_prefixes: list[str] = []
    if READWISE_SKIP_FILE.exists():
        for line in READWISE_SKIP_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                skip_prefixes.append(stripped)
    findings: list[Finding] = []
    ok_notes = [n for n in notes if n.integrity_ok()]
    for note in ok_notes:
        for claim in note.claims:
            for a in claim.anchors:
                atype = a.fields.get("_anchor_type", "")
                aid = a.fields.get("_anchor_id", "")
                if atype in ("url", "gist") and "readwise" not in a.fields and not any(d in aid for d in skip_prefixes):
                    findings.append(
                        Finding(
                            "WARN",
                            "readwise-missing",
                            note.path.as_posix(),
                            f"[C{claim.number}] {atype}: anchor at line {a.line_no} "
                            f"has no `readwise:` field — evidence harder to retrieve "
                            f"if the URL goes down (save to Readwise with tag "
                            f"`anchor-evidence` and backfill the document ID)",
                        )
                    )
    return findings


def check_shadow_drift(notes: list[WikiNote]) -> list[Finding]:
    """Warn on missing/stale localized wiki shadows; no configured shadows is a no-op.

    A 60-second grace period tolerates same-workflow filesystem timestamp jitter.
    """
    findings: list[Finding] = []
    GRACE_SECONDS = 60
    shadow_dirs = localized_shadow_dirs()
    if not shadow_dirs:
        return findings

    for note in notes:
        if not note.title:
            continue
        for shadow_dir in shadow_dirs:
            sh_path = shadow_dir / note.path.relative_to(WIKI_DIR)
            lang_tag = shadow_dir.name
            if not sh_path.exists():
                findings.append(
                    Finding(
                        "WARN",
                        "shadow-missing",
                        note.path.as_posix(),
                        f"no {lang_tag} shadow at `{sh_path.as_posix()}` — "
                        f"re-run /promote Phase 4 or regenerate manually",
                    )
                )
                continue
            en_mtime = note.path.stat().st_mtime
            sh_mtime = sh_path.stat().st_mtime
            if en_mtime > sh_mtime + GRACE_SECONDS:
                findings.append(
                    Finding(
                        "WARN",
                        "shadow-stale",
                        note.path.as_posix(),
                        f"{lang_tag} shadow `{sh_path.as_posix()}` is older than the English source — "
                        f"re-translate to keep the localized reading copy in sync",
                    )
            )
    return findings


def run_lints(notes: list[WikiNote]) -> list[Finding]:
    findings: list[Finding] = []
    # Resolve @cite targets so dangling-cite errors land on the source note
    # before we read parse_errors. trust.py's scoring does this implicitly;
    # lint.py calls it explicitly because we don't score here.
    _resolve_cites(notes)
    findings.extend(check_parse_errors(notes))
    findings.extend(check_duplicate_titles(notes))
    findings.extend(check_slug_alignment(notes))
    findings.extend(check_graph_topology(notes))
    findings.extend(check_readwise_backfill(notes))
    findings.extend(check_unfounded_terms(notes))
    findings.extend(check_shadow_drift(notes))
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), f.code, f.where))
    return findings


def format_table(findings: list[Finding]) -> str:
    return _findings.format_table(
        findings,
        empty="lint: clean (no findings)\n",
        label="lint report",
        separator=" — ",
    )


def format_json(findings: list[Finding]) -> str:
    return _findings.format_json(findings, wiki_dir=WIKI_DIR.as_posix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/lint.py",
        description="Corpus-level structural lints over zk/wiki/.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON for orchestrator consumption.",
    )
    args = parser.parse_args(argv)

    try:
        notes = load_wiki(date.today(), only=None)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    findings = run_lints(notes)

    if args.json:
        sys.stdout.write(format_json(findings))
    else:
        sys.stdout.write(format_table(findings))

    return 1 if any(f.severity == "ERROR" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
