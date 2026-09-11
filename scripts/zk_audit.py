#!/usr/bin/env python3
"""Read-only post-ingestion hygiene audit of $OV; heuristics, not repair authority.

Checks missing domain READMEs/digests, archive overlap, root markdown orphans
(except README.md), empty markdown, and suspicious top-level directories.
Archive empty stubs are counted, not individually listed, to avoid drowning
current ingestion debt. Individual checks explain their false-positive bias.

Run `uv run scripts/zk_audit.py [--json]` for a human/JSON report. Advisory
findings exit 0; IO errors exit 2. `_paths.vault_root()` requires $OV, with no
relative fallback; domain names are discovered, never hardcoded.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import tier, tier_segments, vault_root, wiki_dirs  # type: ignore[import-not-found]  # noqa: E402

OV = vault_root()

# Registered non-ingestion tiers have no working-domain raw/digest contract.
_NON_INGESTION_DOMAINS = {"assets", "profile", "readwise"}
_PRIVATE_COMPONENT_TIERS = {"private_skills", "private_agents", "private_routines", "private_tools"}
for _name in (
    "meta", "agent_findings", "archive", "cache", "daily_notes", "gtd",
    "papers", "preprints", "reflections", "research", "sessions", "wip", "zettelm",
    "inbox", "routine_prompts", "private_skills", "private_agents", "private_routines", "private_tools",
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
            + self.empty_md_archive_count
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

        if _is_hidden(name):
            continue

        try:
            entries = list(child.iterdir())
        except OSError:
            continue
        if not entries:
            out.append(Finding("suspicious_dirs", _rel(child) + "/", "empty directory"))
            continue

        if name in _NON_INGESTION_DOMAINS:
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
    arch = report.empty_md_archive_count
    actionable = total - arch
    if arch:
        summary = (
            f"Summary: 6 categories, {actionable} actionable + {arch} archive-aggregated "
            f"= {total} total finding(s). Audit is advisory; no $OV content was modified."
        )
    else:
        summary = (
            f"Summary: 6 categories, {total} total finding(s). "
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
    args = parser.parse_args(argv)

    if not OV.is_dir():
        msg = f"zk_audit: {OV} is not a vault directory"
        if args.json:
            print(json.dumps({"vault": OV.as_posix(), "error": "not_directory" if OV.exists() else "missing"}, indent=2))
        else:
            sys.stderr.write(msg + "\n")
        return 2

    report = run_audit()

    if args.json:
        sys.stdout.write(format_json(report))
    else:
        sys.stdout.write(format_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
