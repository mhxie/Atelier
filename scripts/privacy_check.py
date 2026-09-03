#!/usr/bin/env python3
"""
privacy_check.py: Detect private identifiers in public-bound repository files.

Terms come from the private-entity index `scripts/privacy_index.py` builds
at `$OV/_meta/privacy_index.json` (directory names and paths, filename stems,
wiki-link targets, routine and feature registries, note frontmatter, profile
proper nouns; each with provenance), rebuilt automatically when missing or a
day old. Two rules run over every public-bound source:

  1. Term rule: any indexed term, case-insensitive with word boundaries.
  2. Path rule: any path-shaped token that names a real directory under a
     content tier of `$OV` (public tier segments from harness/paths.toml are
     never hits). This is what catches `research/<private-dir>/` in prose.

Optional `profile/private_terms.txt` (one phrase per line) and
`profile/private_slugs.txt` (single words) still add explicit terms for what
no vault source can derive.

The scanner reads public-bound pathnames, working-tree content, and staged
index blobs when they differ. A filename-only or partially staged leak
therefore cannot hide behind a cleaned-up working copy.

Auto-skip rules (all fully automated):
  - Single ASCII words from wiki-links (too generic: Reflect, Protocol).
  - File paths (contain `/`), dates, noise patterns.
  - Explicit opt-out via `privacy_allowlist.txt` for edge cases.

Existing public filenames are not automatically trusted. If a private title is
also deliberately public, it must be named in `privacy_allowlist.txt`. This
keeps an earlier leak from silently exempting itself forever.

CLI:
    uv run scripts/privacy_check.py                   human report
    uv run scripts/privacy_check.py --json            machine-readable output
    uv run scripts/privacy_check.py --range A..B      scan every commit in a
                                                      history range (pre-push)
    uv run scripts/privacy_check.py --why "<term>"    provenance of a term
    uv run scripts/privacy_check.py --rebuild-index   refresh the index first
    uv run scripts/privacy_check.py --allow-empty-ov  exit 0 when $OV is unset

Exit code: 0 if no hits, 1 if any hit (treat as ERROR), 2 on IO error or
when the gate cannot meaningfully run (missing/empty $OV without
--allow-empty-ov).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git import git_paths  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = REPO_ROOT / "scripts" / "privacy_allowlist.txt"
PRIVATE_SLUGS = REPO_ROOT / "profile" / "private_slugs.txt"
PRIVATE_TERMS = REPO_ROOT / "profile" / "private_terms.txt"

_INFRA_DIRS = {"cache", "assets", ".obsidian"}


def _discover_private_dirs(root: Path) -> list[str]:
    """Auto-discover content subdirectories under $OV/.

    Skips infrastructure dirs (cache mirrors, binary assets, editor
    config) that don't contain user-authored private identifiers.
    """
    if not root.is_dir():
        return []
    return [
        p.name for p in sorted(root.iterdir())
        if p.is_dir() and p.name not in _INFRA_DIRS and not p.name.startswith(".")
    ]

SKIP_STEMS = {"index", "README", "Note Title"}

_WIKILINK_RE = re.compile(r'(?<!\!)\[\[([^\]]+)\]\]')
_DATE_RE = re.compile(r'^\d{4}(-\d{2}(-\d{2})?)?$')
_NOISE_RE = re.compile(r'^[.\s]+$')
_NUMERIC_RE = re.compile(r'^[\d\s,.\-]+$')
_SINGLE_ASCII_WORD_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _is_allowlisted(term: str, allowlist: set[str]) -> bool:
    folded = term.casefold()
    return any(folded == entry.casefold() for entry in allowlist)


def _separator_variants(term: str) -> set[str]:
    """Return literal and space-normalized forms for slug-shaped titles."""
    normalized = re.sub(r"[-_]+", " ", term).strip()
    return {candidate for candidate in (term.strip(), normalized) if candidate}


def load_allowlist() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    out: set[str] = set()
    for line in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s)
    return out


def load_private_slugs() -> set[str]:
    """Load single-word private slugs (employer names, codenames) from a
    gitignored sidecar list.

    The multi-word filename-stem and wikilink heuristics deliberately skip
    single ASCII words to avoid flagging system vocabulary (e.g., "Reflect",
    "Protocol"). That floor lets through employer slugs and project
    codenames that happen to be one word. The user maintains this list
    explicitly because no heuristic can reliably tell a generic word from
    a private proper noun. File is gitignored under `profile/`; absent
    file means no slugs configured.
    """
    if not PRIVATE_SLUGS.exists():
        return set()
    out: set[str] = set()
    for line in PRIVATE_SLUGS.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s.lower())
    return out


def load_private_terms() -> set[str]:
    """Load exact private literals from the gitignored profile sidecar.

    This list covers identifiers and preference phrases that filename and
    wikilink discovery cannot infer reliably. Matching is case-insensitive.
    """
    if not PRIVATE_TERMS.exists():
        return set()
    out: set[str] = set()
    for line in PRIVATE_TERMS.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        out.add(term)
    return out


def collect_titles(root: Path, allowlist: set[str], dirs: list[str]) -> list[str]:
    titles: set[str] = set()
    for sub in dirs:
        p = root / sub
        if not p.exists():
            continue
        for f in p.rglob("*.md"):
            stem = f.stem
            variants = _separator_variants(stem)
            normalized = re.sub(r"[-_]+", " ", stem).strip()
            if stem in SKIP_STEMS or len(normalized.split()) < 2:
                continue
            if _DATE_RE.fullmatch(stem) or _NUMERIC_RE.fullmatch(normalized):
                continue
            for candidate in variants:
                if not _is_allowlisted(candidate, allowlist):
                    titles.add(candidate)
    return sorted(titles)


def _is_private_wikilink(target: str) -> bool:
    """Heuristic: is this wiki-link target likely a private identifier?

    Accepts multi-word targets (person names, note titles) and non-ASCII
    targets with enough specificity (3+ chars). Rejects single ASCII
    words, file paths, dates, noise, and short generic CJK terms.
    """
    if len(target) < 2:
        return False
    if _DATE_RE.match(target) or _NOISE_RE.match(target):
        return False
    if _NUMERIC_RE.match(target):
        return False
    has_non_ascii = any(ord(c) > 127 for c in target)
    if has_non_ascii:
        non_ascii_count = sum(1 for c in target if ord(c) > 127)
        return non_ascii_count >= 3
    return len(target.split()) >= 2


def _wikilink_candidates(raw_target: str) -> set[str]:
    """Extract basename and separator variants from an Obsidian target."""
    target = raw_target.split("|", 1)[0].split("#", 1)[0].strip()
    if not target:
        return set()
    target = target.replace("\\", "/").rsplit("/", 1)[-1]
    if target.lower().endswith(".md"):
        target = target[:-3]
    return _separator_variants(target)


def collect_wikilinks(root: Path, allowlist: set[str], dirs: list[str]) -> set[str]:
    """Extract [[wiki-link]] targets from vault files as private terms.

    Catches people names, note references, and concepts that may not
    have their own files but still appear as identifiers in the vault.
    Filters to multi-word ASCII targets and any non-ASCII targets to
    avoid false positives on single-word system terms.
    """
    targets: set[str] = set()
    for sub in dirs:
        p = root / sub
        if not p.is_dir():
            continue
        for f in p.rglob("*.md"):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in _WIKILINK_RE.finditer(text):
                for target in _wikilink_candidates(m.group(1)):
                    if target in SKIP_STEMS or _is_allowlisted(target, allowlist):
                        continue
                    if _is_private_wikilink(target):
                        targets.add(target)
    return targets


def _git_paths(args: list[str], repo_root: Path) -> list[str]:
    try:
        return git_paths(repo_root, *args)
    except (RuntimeError, OSError) as exc:
        sys.stderr.write(f"privacy_check: {exc}\n")
        raise SystemExit(2)


def tracked_files(repo_root: Path = REPO_ROOT) -> list[str]:
    """Files tracked by git PLUS untracked-but-not-ignored files.

    The privacy gate cares about content about to enter the repo, not just
    content already in HEAD. A brand-new file (e.g., a fresh command under
    .claude/commands/) must be scanned before it is staged, otherwise the
    gate has a trivial bypass: add a leak in a new file and it is invisible
    to `git ls-files`.
    """
    tracked = _git_paths(["ls-files"], repo_root)
    untracked = _git_paths(["ls-files", "-o", "--exclude-standard"], repo_root)
    return sorted(set(tracked) | set(untracked))


def staged_files(repo_root: Path = REPO_ROOT) -> list[str]:
    """Index paths whose staged blob will survive the next commit."""
    return _git_paths(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        repo_root,
    )


def _worktree_text(repo_root: Path, relative: str) -> str | None:
    path = repo_root / relative
    try:
        if path.is_symlink():
            return os.readlink(path)
        return path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return None


def _index_text(repo_root: Path, relative: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f":{relative}"],
        cwd=repo_root,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="ignore")


def content_sources(
    files: list[str], repo_root: Path = REPO_ROOT
) -> list[tuple[str, str, str]]:
    """Return `(path, source, text)` for worktree and divergent staged blobs."""
    sources: list[tuple[str, str, str]] = []
    worktree: dict[str, str] = {}
    for relative in files:
        text = _worktree_text(repo_root, relative)
        if text is None:
            continue
        worktree[relative] = text
        sources.append((relative, "worktree", text))
    for relative in staged_files(repo_root):
        text = _index_text(repo_root, relative)
        if text is not None and text != worktree.get(relative):
            sources.append((relative, "index", text))
    return sources


def path_sources(files: list[str]) -> list[tuple[str, str, str]]:
    """Expose normalized public-bound pathnames to the literal scanner."""
    return [
        (
            relative,
            "path",
            re.sub(r"[-_/\\]+", " ", relative),
        )
        for relative in files
    ]


_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)")


def scan_vault_paths(paths: list[str], sources: list[tuple[str, str, str]]) -> list[dict]:
    """Path rule: a path-shaped token whose prefix is a private vault directory."""
    if not paths:
        return []
    private = {p.strip("/").casefold() for p in paths}
    hits: list[dict] = []
    for relative, source, content in sources:
        if source == "path":
            continue
        seen: set[str] = set()
        for i, line in enumerate(content.splitlines(), 1):
            for m in _PATH_TOKEN_RE.finditer(line):
                token = m.group(1).strip("/").casefold()
                parts = token.split("/")
                for k in range(1, len(parts) + 1):
                    prefix = "/".join(parts[:k])
                    if prefix in private:
                        if prefix not in seen:
                            seen.add(prefix)
                            hits.append({"file": relative, "line": i, "private_title": prefix, "source": source,
                                         "rule": "vault-path", "why": "a directory under a content tier of $OV"})
                        break
    return hits


def range_sources(rev_range: str, repo_root: Path = REPO_ROOT) -> list[tuple[str, str, str]]:
    """`(path, source, text)` for every file each commit in `rev_range` touched.

    Intermediate commits count: a name added in one commit and removed two
    commits later still ships in history.
    """
    commits = subprocess.run(
        ["git", "rev-list", "--reverse", rev_range], cwd=repo_root, capture_output=True, text=True
    ).stdout.split()
    sources: list[tuple[str, str, str]] = []
    for commit in commits:
        short = commit[:7]
        listing = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "--diff-filter=AM", commit],
            cwd=repo_root, capture_output=True, text=True,
        ).stdout.splitlines()
        for relative in (line.strip() for line in listing if line.strip()):
            blob = subprocess.run(["git", "show", f"{commit}:{relative}"], cwd=repo_root, capture_output=True)
            if blob.returncode != 0:
                continue
            try:
                text = blob.stdout.decode("utf-8")
            except UnicodeDecodeError:
                continue
            sources.append((relative, f"history:{short}", text))
            sources.append((relative, "path", re.sub(r"[-_/\\]+", " ", relative)))
    return sources


def scan(terms: list[str], sources: list[tuple[str, str, str]]) -> list[dict]:
    """Case-insensitive scan with word boundaries for ASCII terms."""
    hits: list[dict] = []
    normalized_terms = [
        (
            term,
            term.casefold(),
            re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
            if term.isascii()
            else None,
        )
        for term in terms
    ]
    for relative, source, content in sources:
        folded_content = content.casefold()
        lines: list[str] | None = None
        folded_lines: list[str] | None = None
        for term, folded_term, pattern in normalized_terms:
            if folded_term not in folded_content:
                continue
            if lines is None:
                lines = content.splitlines()
                folded_lines = [line.casefold() for line in lines]
            assert folded_lines is not None
            for i, (line, folded_line) in enumerate(zip(lines, folded_lines), 1):
                matched = pattern.search(line) is not None if pattern else folded_term in folded_line
                if matched:
                    hits.append({
                        "file": relative,
                        "line": i,
                        "private_title": term,
                        "source": source,
                    })
                    break
    return hits


def scan_slugs(
    slugs: set[str], sources: list[tuple[str, str, str]]
) -> list[dict]:
    """Scan files for single-word private slugs.

    Case-insensitive, ASCII-word-boundary aware so a slug "foo"
    matches "Foo" and "foo's" but not "foobar" or "tofoo". Private slugs
    are typically employer names or codenames that need this stricter
    boundary check; the multi-word `scan` uses substring match because
    multi-word phrases rarely appear inside other words.
    """
    if not slugs:
        return []
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(s) for s in sorted(slugs))
        + r")(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    hits: list[dict] = []
    for relative, source, content in sources:
        seen_in_file: set[str] = set()
        for i, line in enumerate(content.splitlines(), 1):
            for m in pattern.finditer(line):
                slug = m.group(1).lower()
                if slug in seen_in_file:
                    continue
                seen_in_file.add(slug)
                hits.append({
                    "file": relative,
                    "line": i,
                    "private_title": slug,
                    "source": source,
                })
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scripts/privacy_check.py",
        description=(
            "Scan public-bound files and divergent staged blobs for private "
            "vault identifiers and locally declared private terms."
        ),
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON output.")
    ap.add_argument("--range", default=None, metavar="A..B", help="Scan every commit in a git history range instead of the working tree.")
    ap.add_argument("--why", default=None, metavar="TERM", help="Explain where a term comes from (or why it is not indexed) and exit.")
    ap.add_argument("--rebuild-index", action="store_true", help="Rebuild $OV/_meta/privacy_index.json before scanning.")
    ap.add_argument(
        "--allow-empty-ov",
        action="store_true",
        help=(
            "Exit 0 when the gate would scan vacuously: either $OV is "
            "missing, OR $OV exists but has no private dirs, private terms, "
            "or private slugs. Without this flag, both cases exit 2 "
            "to avoid a placebo green light for fresh clones."
        ),
    )
    args = ap.parse_args(argv)

    # Resolve $OV after argparse so --allow-empty-ov / --json reach the
    # soft-skip path when OV is unset (otherwise the helper would exit during
    # path resolution before either flag is consulted).
    ov_env = os.environ.get("OV")
    OV = Path(ov_env).expanduser().resolve() if ov_env else None
    if OV is None or not OV.exists():
        ov_label = OV.as_posix() if OV is not None else "$OV (unset)"
        msg = (
            f"privacy_check: {ov_label} does not exist; cannot scan. "
            "Set $OV or pass --allow-empty-ov to acknowledge an empty gate."
        )
        if args.json:
            print(json.dumps(
                {"action": "soft_skip", "reason": "vault not available",
                 "zk_missing": True, "titles_scanned": 0, "hits": []},
                indent=2,
            ))
        else:
            sys.stderr.write(msg + "\n")
        return 0 if args.allow_empty_ov else 2

    allowlist = load_allowlist()
    private_slugs = load_private_slugs()
    private_terms = load_private_terms()
    coverage_warnings: list[str] = []
    import privacy_index

    if args.why:
        print(json.dumps(privacy_index.explain(privacy_index.load_or_build(OV, allowlist=allowlist), args.why), ensure_ascii=False, indent=1))
        return 0
    dirs = _discover_private_dirs(OV)
    if not dirs and not private_slugs and not private_terms and not args.allow_empty_ov:
        msg = (
            f"privacy_check: $OV={OV} contains no private dirs and no "
            "private term sidecar is configured; gate would pass vacuously. "
            "Pass --allow-empty-ov to acknowledge."
        )
        if args.json:
            print(json.dumps(
                {
                    "action": "soft_skip",
                    "reason": "no private dirs to scan",
                    "vacuous_gate": True,
                    "ov_dir": OV.as_posix(),
                    "titles_scanned": 0,
                    "hits": [],
                },
                indent=2,
            ))
        else:
            sys.stderr.write(msg + "\n")
        return 2
    index = privacy_index.load_or_build(OV, force=args.rebuild_index, allowlist=allowlist)
    index_terms: dict[str, dict] = index.get("terms", {})
    counts = index.get("counts", {})
    titles = [t for t, e in index_terms.items() if "stem" in e["kinds"]]
    wikilinks = {t for t, e in index_terms.items() if "wikilink" in e["kinds"]}
    if args.range:
        sources = range_sources(args.range)
        if not sources:
            coverage_warnings.append(f"range {args.range} touched no readable files")
    else:
        files = tracked_files()
        sources = content_sources(files)
        sources.extend(path_sources(files))
    explicit = set(index_terms) | private_terms
    slug_terms = private_slugs | {t.casefold() for t in explicit if _SINGLE_ASCII_WORD_RE.fullmatch(t)}
    phrase_terms = sorted(t for t in explicit if not _SINGLE_ASCII_WORD_RE.fullmatch(t))
    hits = scan(phrase_terms, sources) if phrase_terms else []
    hits.extend(scan_slugs(slug_terms, sources))
    hits.extend(scan_vault_paths(index.get("paths", []), sources))
    for hit in hits:
        if "why" in hit:
            continue
        entry = next((e for t, e in index_terms.items() if t.casefold() == str(hit["private_title"]).casefold()), None)
        hit["rule"] = "term"
        hit["why"] = (f"{'/'.join(entry['kinds'])}: {entry['sources'][0]}" if entry and entry.get("sources") else "explicit private term or slug")

    if args.json:
        print(json.dumps({
            "action": "abort" if hits else "proceed",
            "ov_dir": OV.as_posix(),
            "range": args.range,
            "index_built": index.get("built"),
            "index_counts": counts,
            "paths_scanned": len(index.get("paths", [])),
            "filename_stems": len(titles),
            "wikilink_targets": len(wikilinks),
            "private_slugs": len(private_slugs),
            "private_terms": len(private_terms),
            "private_terms_configured": PRIVATE_TERMS.exists(),
            "terms_scanned": len(phrase_terms) + len(slug_terms),
            "allowlist_size": len(allowlist),
            "coverage_warnings": coverage_warnings,
            "hit_count": len(hits),
            "hits": hits,
        }, indent=2, ensure_ascii=False))
    else:
        if not hits:
            kinds = ", ".join(f"{counts.get(k, 0)} {k}" for k in ("dir", "stem", "wikilink", "registry", "frontmatter", "profile"))
            scope = f"history {args.range}" if args.range else "working tree"
            print(
                f"privacy_check: clean ({scope}; {len(phrase_terms) + len(slug_terms)} terms "
                f"[{kinds}, {len(private_slugs)} slugs, {len(private_terms)} explicit] + "
                f"{len(index.get('paths', []))} vault paths, 0 leaks)"
            )
            for warning in coverage_warnings:
                print(f"privacy_check: coverage warning: {warning}", file=sys.stderr)
            return 0
        files_hit = sorted({h["file"] for h in hits})
        print(
            f"privacy_check: {len(hits)} leak(s) across "
            f"{len(files_hit)} file(s)"
        )
        for h in hits:
            source = {
                "index": " [staged index]",
                "path": " [pathname]",
                "worktree": "",
            }.get(h.get("source"), f" [{h.get('source', 'unknown')}]")
            print(f"  {h['file']}:{h['line']}{source}: {h['private_title']!r}  <- {h.get('why', '')}")
        for warning in coverage_warnings:
            print(f"privacy_check: coverage warning: {warning}", file=sys.stderr)
        print()
        print(
            "Each line shows a private identifier from your $OV vault appearing in a "
            "public-bound file, with the source that indexed it. Replace it with a "
            "placeholder or registry indirection; add it to scripts/privacy_allowlist.txt "
            "only if the exposure is deliberate. `--why '<term>'` explains a term."
        )

    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
