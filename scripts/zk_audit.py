#!/usr/bin/env python3
"""Read-only post-ingestion hygiene audit of $OV; heuristics, not repair authority.

Checks missing domain READMEs/digests, archive overlap, root markdown orphans
(except README.md), empty markdown, suspicious top-level directories, and the
vault layout: a Git work tree outside file-sync folders whose raw/ and secure/
folders (and root cache) are links into raw_store, plus Reflect titles that
fall back to a filename another note shares. Archive empty stubs and archive
duplicate titles are counted, not individually listed, to avoid drowning
current ingestion debt.
Individual checks explain their false-positive bias.

Run `uv run scripts/zk_audit.py [--json]` for a human/JSON report. Advisory
findings exit 0; IO errors exit 2. `--fix-links` only creates missing links
into raw_store; it never moves or deletes. `_paths.vault_root()` requires $OV,
with no relative fallback; domain names are discovered, never hardcoded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import raw_store, tier, tier_segments, vault_root, wiki_dirs  # type: ignore[import-not-found]  # noqa: E402

OV = vault_root()

# Registered non-ingestion tiers have no working-domain raw/digest contract.
_NON_INGESTION_DOMAINS = {"assets", "profile", "readwise"}
_PRIVATE_COMPONENT_TIERS = {"private_skills", "private_agents", "private_routines", "private_tools"}
for _name in (
    "meta", "agent_findings", "archive", "cache", "daily_notes", "gtd",
    "papers", "preprints", "reflections", "research", "sessions", "wip",
    "inbox", "notes", "audio_memos", "routine_prompts", "private_skills", "private_agents", "private_routines", "private_tools",
):
    _path = tier(_name)
    if _path.is_relative_to(OV) and _path.relative_to(OV).parts:
        _parts = _path.relative_to(OV).parts
        if len(_parts) == 1 or _name in _PRIVATE_COMPONENT_TIERS:
            _NON_INGESTION_DOMAINS.add(_parts[0])
for _path in wiki_dirs():
    if _path.is_relative_to(OV) and len(_path.relative_to(OV).parts) == 1:
        _NON_INGESTION_DOMAINS.add(_path.relative_to(OV).parts[0])

# Pattern: directory ending in " 2", " 3", " (2)", etc. Finder produces
# these when iCloud or Drive sync detects a phantom duplicate.
_FINDER_DUP_RE = re.compile(r"\s(?:\d+|\(\d+\))$")

# Folders that live in raw_store and appear in the vault as links; Git and
# Reflect skip them. A .git under a file-sync client risks object corruption.
_STORE_FOLDERS = {"raw", "secure"}
_SYNC_ROOTS = ("/Library/CloudStorage/", "/Library/Mobile Documents/")


@dataclass
class Finding:
    category: str
    where: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"category": self.category, "where": self.where, "detail": self.detail}


@dataclass
class Report:
    vault: str
    missing_readmes: list[Finding] = field(default_factory=list)
    raw_no_digest: list[Finding] = field(default_factory=list)
    archive_overlap: list[Finding] = field(default_factory=list)
    root_orphans: list[Finding] = field(default_factory=list)
    empty_md: list[Finding] = field(default_factory=list)
    empty_md_archive_count: int = 0
    suspicious_dirs: list[Finding] = field(default_factory=list)
    layout: list[Finding] = field(default_factory=list)
    duplicate_titles: list[Finding] = field(default_factory=list)
    duplicate_titles_archive_count: int = 0

    def total(self) -> int:
        # Includes the aggregated archive empty-stub count so a JSON
        # consumer that keys off `total` does not see 0 when the only
        # debt is archive stubs. The human summary line breaks the
        # number down into actionable + archive-aggregated components.
        return (
            len(self.missing_readmes)
            + len(self.raw_no_digest)
            + len(self.archive_overlap)
            + len(self.root_orphans)
            + len(self.empty_md)
            + len(self.suspicious_dirs)
            + len(self.layout)
            + len(self.duplicate_titles)
            + self.empty_md_archive_count
            + self.duplicate_titles_archive_count
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "vault": self.vault,
            "categories": {
                "missing_readmes": [f.to_dict() for f in self.missing_readmes],
                "raw_no_digest": [f.to_dict() for f in self.raw_no_digest],
                "archive_overlap": [f.to_dict() for f in self.archive_overlap],
                "root_orphans": [f.to_dict() for f in self.root_orphans],
                "empty_md": [f.to_dict() for f in self.empty_md],
                "empty_md_archive_count": self.empty_md_archive_count,
                "suspicious_dirs": [f.to_dict() for f in self.suspicious_dirs],
                "layout": [f.to_dict() for f in self.layout],
                "duplicate_titles": [f.to_dict() for f in self.duplicate_titles],
                "duplicate_titles_archive_count": self.duplicate_titles_archive_count,
            },
            "total": self.total(),
        }


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def discover_working_domains(root: Path) -> list[Path]:
    """Working-tier domains: top-level directories under $OV that are
    neither hidden, infrastructure, nor a different-tier home.

    A "domain" here is the unit the ingestion protocol mints: e.g.,
    auto/, career/, finance/. Whether it has a raw/ subdir is incidental
    (skeleton domains without raw/ still need a README).
    """
    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if _is_hidden(child.name):
            continue
        if child.name in _NON_INGESTION_DOMAINS:
            continue
        out.append(child)
    return out


def check_missing_readmes(domains: list[Path]) -> list[Finding]:
    """Protocol mandates one README per working-tier domain."""
    out: list[Finding] = []
    for d in domains:
        if not (d / "README.md").is_file():
            out.append(Finding("missing_readmes", _rel(d) + "/"))
    return out


def check_raw_without_digest(domains: list[Path]) -> list[Finding]:
    """For each `<domain>/raw/<sub>/`, look for any .md file in the
    working tier (anywhere under <domain>/ but not under raw/) whose
    text mentions <sub> by name or as `raw/<sub>`.

    Heuristic, not a parser: a digest can reference its source many
    ways (wikilink, relative path, prose mention). We accept any
    literal substring match. False negatives are possible (digest
    refers to source by a synonym); false positives are unlikely
    (matching on the exact subdir name).
    """
    out: list[Finding] = []
    for d in domains:
        raw_dir = d / "raw"
        if not raw_dir.is_dir():
            continue
        subs = [p for p in sorted(raw_dir.iterdir()) if p.is_dir()]
        if not subs:
            continue

        # Collect digest text from .md files in the working tier
        # (excluding raw/ itself, and README.md which documents the raw
        # layout but is not a digest — counting it would mask domains
        # that have only README + raw/ with no real digest).
        digest_text = ""
        for md in d.rglob("*.md"):
            if "raw" in md.relative_to(d).parts:
                continue
            if md.name == "README.md":
                continue
            try:
                digest_text += md.read_text(encoding="utf-8", errors="ignore")
                digest_text += "\n"
            except OSError:
                continue

        for sub in subs:
            name = sub.name
            if name in digest_text or f"raw/{name}" in digest_text:
                continue
            out.append(
                Finding(
                    "raw_no_digest",
                    _rel(sub) + "/",
                    f"no .md in {_rel(d)}/ references {name!r} or raw/{name}",
                )
            )
    return out


def _normalize_token(s: str) -> str:
    """Lowercase, strip an `-admin` / `_admin` / ` admin` suffix.

    Lets `health-admin` overlap match `health/`, `finance-admin` match
    `finance/`, etc. without enumerating user-specific suffixes.
    """
    s = s.strip().lower()
    for suffix in ("-admin", "_admin", " admin"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


def check_archive_overlap(root: Path, domains: list[Path]) -> list[Finding]:
    """Surface archive subtrees whose normalized name overlaps a current
    working-tier domain. Pure surfacing: do not propose a target;
    consolidation is per-subtree user judgment on the next manual pass.

    Walks two archive levels: `archive/<bucket>/` and
    `archive/<bucket>/<sub>/`. Matches by normalized substring in either
    direction (working-tier name in archive name, or vice versa).
    """
    out: list[Finding] = []
    archive = root / tier_segments()["archive"]
    if not archive.is_dir():
        return out

    domain_names = {d.name.lower() for d in domains}
    domain_norm = {_normalize_token(d.name): d.name for d in domains}

    candidates: list[Path] = []
    for bucket in sorted(archive.iterdir()):
        if not bucket.is_dir() or _is_hidden(bucket.name):
            continue
        candidates.append(bucket)
        for sub in sorted(bucket.iterdir()):
            if sub.is_dir() and not _is_hidden(sub.name):
                candidates.append(sub)

    for path in candidates:
        norm = _normalize_token(path.name)
        if not norm:
            continue
        match: str | None = None
        if norm in domain_norm:
            match = domain_norm[norm]
        else:
            for dnorm, dname in domain_norm.items():
                if dnorm and (dnorm in norm or norm in dnorm):
                    match = dname
                    break
            if match is None:
                for dn in domain_names:
                    if dn and (dn in path.name.lower() or path.name.lower() in dn):
                        match = dn
                        break
        if match is None:
            continue
        out.append(
            Finding(
                "archive_overlap",
                _rel(path) + "/",
                f"overlaps working-tier domain {match!r}",
            )
        )
    return out


def check_root_orphans(root: Path) -> tuple[list[Finding], list[Finding], int]:
    """Return (root orphans except README.md, listed empty files, archive-empty count).

    Archive stubs stay aggregate-only so old ingestion debt cannot drown current gaps.
    """
    if not root.is_dir():
        return [], [], 0

    root_orphans: list[Finding] = []
    for child in sorted(root.iterdir()):
        if not child.is_file():
            continue
        if child.suffix.lower() != ".md":
            continue
        if child.name == "README.md":
            continue
        size = child.stat().st_size
        suffix = " (0 bytes)" if size == 0 else f" ({size} bytes)"
        root_orphans.append(Finding("root_orphans", _rel(child), f"unexpected at vault root{suffix}"))

    empty_listed: list[Finding] = []
    empty_archive = 0
    archive = root / tier_segments()["archive"]
    for path in root.rglob("*.md"):
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        try:
            if path.stat().st_size != 0:
                continue
        except OSError:
            continue
        if path.is_relative_to(archive):
            empty_archive += 1
        else:
            empty_listed.append(Finding("empty_md", _rel(path)))
    return root_orphans, empty_listed, empty_archive


def check_suspicious_dirs(root: Path) -> list[Finding]:
    """Find top-level Finder duplicates, empty dirs, and eligible README-less skeletons.

    Three entries clear a skeleton: avoid flagging a real domain still being built.
    """
    out: list[Finding] = []
    if not root.is_dir():
        return out

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name

        if _FINDER_DUP_RE.search(name):
            out.append(
                Finding(
                    "suspicious_dirs",
                    _rel(child) + "/",
                    "Finder-duplicate name pattern (` <n>` or ` (<n>)`)",
                )
            )
            continue

        if _is_hidden(name) or name in _NON_INGESTION_DOMAINS:
            continue

        try:
            entries = list(child.iterdir())
        except OSError:
            continue
        if not entries:
            out.append(Finding("suspicious_dirs", _rel(child) + "/", "empty directory"))
            continue

        has_readme = any(p.name == "README.md" for p in entries)
        if not has_readme and len(entries) < 3:
            out.append(
                Finding(
                    "suspicious_dirs",
                    _rel(child) + "/",
                    f"no README and only {len(entries)} entry(ies) (skeleton or abandoned?)",
                )
            )

    return out


def _store_folders(store: Path) -> list[str]:
    """Vault-relative raw/ and secure/ folders present in raw_store."""
    out: list[str] = []
    for current, dirnames, _files in os.walk(store):
        rel = Path(current).relative_to(store)
        out += [(rel / d).as_posix() for d in dirnames if d in _STORE_FOLDERS]
        dirnames[:] = sorted(d for d in dirnames if not _is_hidden(d) and d not in _STORE_FOLDERS)
    return sorted(out)


def check_layout(root: Path, store: Path | None) -> list[Finding]:
    """Vault is a Git work tree outside sync folders; store folders are links into raw_store."""
    if not root.is_dir():
        return [Finding("layout", root.as_posix(), "vault root is missing; $OV may be stale")]
    out: list[Finding] = []
    if not (root / ".git").exists():
        out.append(Finding("layout", root.as_posix(), "not a Git work tree; Reflect syncs the vault through Git"))
    elif any(part in root.resolve().as_posix() + "/" for part in _SYNC_ROOTS):
        out.append(Finding("layout", _rel(root / ".git"), ".git inside a file-sync folder"))
    if store is None:
        return out
    if not store.is_dir():
        return out + [Finding("layout", store.as_posix(), "raw_store is missing or unmounted")]
    for current, dirnames, files in os.walk(root):
        here = Path(current)
        dirnames[:] = [d for d in dirnames if not _is_hidden(d)]
        # A dangling link is listed with files, not directories.
        for d in [d for d in dirnames + files if d in _STORE_FOLDERS or (here == root and d == "cache")]:
            path, rel = here / d, (here / d).relative_to(root).as_posix()
            if path.is_symlink() and (Path(os.readlink(path)) != store / rel or not path.is_dir()):
                out.append(Finding("layout", _rel(path), f"link should resolve to {store / rel}"))
            elif not path.is_symlink() and path.is_dir():
                out.append(Finding("layout", _rel(path) + "/", "real folder: local-only and unsynced; move it into raw_store and link it"))
            if d in dirnames:
                dirnames.remove(d)
    for rel in _store_folders(store):
        if not (root / rel).is_symlink() and not (root / rel).exists():
            out.append(Finding("layout", _rel(root / rel), "raw_store folder has no link here; run with --fix-links"))
    return out


def fix_links(root: Path, store: Path) -> list[Path]:
    """Create the links check_layout reports missing; never moves or deletes.

    Only inside a Git work tree, so a stale $OV cannot grow a stray vault.
    """
    made: list[Path] = []
    if not (root / ".git").exists():
        return made
    for rel in _store_folders(store):
        link = root / rel
        if not link.is_symlink() and not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(store / rel)
            made.append(link)
    return made


# Reflect titles a note by frontmatter `title:`, then its first H1, then its
# filename; a fallback title another note shares leaves [[Title]] ambiguous.
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_H1_RE = re.compile(r"^ {0,3}# +(\S.*?)(?:\s+#+)?\s*$")


def _reflect_title(text: str) -> str | None:
    """Frontmatter `title:`, else the first ATX H1 outside code fences."""
    lines = text.splitlines()
    body = 0
    if lines and lines[0].rstrip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.rstrip() == "---":
                body = i + 1
                break
            if line.startswith("title:") and (value := line[6:].strip().strip("\"'").strip()):
                return value
    fenced = False
    for line in lines[body:]:
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced and (m := _H1_RE.match(line)):
            return m.group(1)
    return None


def _title_key(title: str) -> str:
    """Approximate Reflect's foldFallbackTitleKey: NFC, lowercase, collapsed
    whitespace, leading emoji (symbol, joiner, and selector code points) dropped."""
    key = " ".join(unicodedata.normalize("NFC", title).lower().split())
    while key and (unicodedata.category(key[0]) in ("So", "Sk") or key[0] in "‍️"):
        key = key[1:]
    return key.lstrip()


def check_duplicate_titles(root: Path) -> tuple[list[Finding], int]:
    """Reflect-visible notes whose filename-fallback title another note shares.

    Mirrors Reflect's catalog: hidden, .reflectignore'd (folder-name patterns
    only), raw/, and secure/ folders, root daily notes, and symlinks are
    skipped, so secure notes are never read. Setext H1s are
    not parsed, so a note titled only by one is a false positive. Groups whose
    only extra members sit under the archive tier are counted, not listed.
    """
    seg = tier_segments()
    archive, daily = root / seg["archive"], root / seg["daily_notes"]
    ignore = root / ".reflectignore"
    patterns = ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else []
    ignored = {p.strip().strip("/") for p in patterns if p.strip() and not p.lstrip().startswith("#")}
    groups: dict[str, list[tuple[Path, bool]]] = defaultdict(list)
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if path.is_symlink() or path.parent == daily or any(
            _is_hidden(p) or p in ignored or p in _STORE_FOLDERS for p in rel.parts[:-1]
        ):
            continue
        title = _reflect_title(path.read_text(encoding="utf-8", errors="replace"))
        groups[_title_key(title or path.stem)].append((path, title is None))
    out: list[Finding] = []
    archive_only = 0
    for key, notes in sorted(groups.items()):
        if len(notes) < 2 or all(not fallback for _, fallback in notes):
            continue
        if sum(not p.is_relative_to(archive) for p, _ in notes) < 2:
            archive_only += 1
            continue
        out.append(Finding("duplicate_title", key, ", ".join(sorted(_rel(p) for p, _ in notes))))
    return out, archive_only


def _rel(path: Path) -> str:
    """Render a path relative to $OV if possible, else absolute.

    Audit output is for the human reading the report, so showing
    `auto/raw/Tesla...` is friendlier than the absolute Drive path.
    """
    try:
        return "$OV/" + path.relative_to(OV).as_posix()
    except ValueError:
        return path.as_posix()


def run_audit() -> Report:
    report = Report(vault=OV.as_posix())
    domains = discover_working_domains(OV)
    report.missing_readmes = check_missing_readmes(domains)
    report.raw_no_digest = check_raw_without_digest(domains)
    report.archive_overlap = check_archive_overlap(OV, domains)
    report.root_orphans, report.empty_md, report.empty_md_archive_count = check_root_orphans(OV)
    report.suspicious_dirs = check_suspicious_dirs(OV)
    report.layout = check_layout(OV, raw_store())
    report.duplicate_titles, report.duplicate_titles_archive_count = check_duplicate_titles(OV)
    return report


def format_human(report: Report) -> str:
    from datetime import date

    lines: list[str] = []
    lines.append(f"zk_audit  {date.today().isoformat()}")
    lines.append(f"Vault: {report.vault}")
    lines.append("")

    sections = [
        ("[1] Missing READMEs", report.missing_readmes, None),
        (
            "[2] Raw subtrees without apparent digest",
            report.raw_no_digest,
            "heuristic: substring search for the subdir name in working-tier .md text",
        ),
        (
            "[3] Archive <-> working-tier overlap candidates",
            report.archive_overlap,
            "review per subtree; keep, merge into working tier, or rename",
        ),
        ("[4a] Root-level orphan .md files", report.root_orphans, None),
        (
            "[4b] Empty (0-byte) .md files in working tier or root",
            report.empty_md,
            (
                f"+ {report.empty_md_archive_count} empty .md under the registered archive "
                f"(aggregated; pre-ingestion stubs, not new debt)"
                if report.empty_md_archive_count
                else None
            ),
        ),
        ("[5] Suspicious top-level dirs", report.suspicious_dirs, None),
        ("[6] Vault layout", report.layout, "missing links are fixable with --fix-links"),
        (
            "[7] Duplicate Reflect titles",
            report.duplicate_titles,
            (
                f"+ {report.duplicate_titles_archive_count} groups whose other copies are only "
                f"under the registered archive (aggregated)"
                if report.duplicate_titles_archive_count
                else None
            ),
        ),
    ]

    for title, items, note in sections:
        lines.append(f"{title} ({len(items)})")
        if note:
            lines.append(f"    note: {note}")
        if not items:
            lines.append("    (none)")
        else:
            for f in items:
                if f.detail:
                    lines.append(f"  - {f.where}  -- {f.detail}")
                else:
                    lines.append(f"  - {f.where}")
        lines.append("")

    total = report.total()
    arch = report.empty_md_archive_count + report.duplicate_titles_archive_count
    actionable = total - arch
    if arch:
        summary = (
            f"Summary: 8 categories, {actionable} actionable + {arch} archive-aggregated "
            f"= {total} total finding(s). Audit is advisory; no $OV content was modified."
        )
    else:
        summary = (
            f"Summary: 8 categories, {total} total finding(s). "
            "Audit is advisory; no $OV content was modified."
        )
    lines.append(summary)
    lines.append("")
    return "\n".join(lines)


def format_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/zk_audit.py",
        description="Post-ingestion hygiene audit for the $OV vault.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    parser.add_argument("--fix-links", action="store_true", help="Create missing links into raw_store, then audit.")
    args = parser.parse_args(argv)

    if not OV.is_dir():
        msg = f"zk_audit: {OV} is not a vault directory"
        if args.json:
            print(json.dumps({"vault": OV.as_posix(), "error": "not_directory" if OV.exists() else "missing"}, indent=2))
        else:
            sys.stderr.write(msg + "\n")
        return 2

    if args.fix_links and (store := raw_store()) is not None and store.is_dir():
        for link in fix_links(OV, store):
            sys.stderr.write(f"zk_audit: linked {_rel(link)}\n")
    report = run_audit()

    if args.json:
        sys.stdout.write(format_json(report))
    else:
        sys.stdout.write(format_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
