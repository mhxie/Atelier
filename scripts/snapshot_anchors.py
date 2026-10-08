#!/usr/bin/env python3
"""Snapshot URL and gist evidence to Readwise and backfill document IDs.

Dry-run is the default; ``--apply`` performs the remote save and local marker
update. Exit status distinguishes complete, partial, and CLI/IO failure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import atomic_write, tier  # type: ignore[import-not-found]  # noqa: E402

WIKI_DIR = tier("wiki")

ANCHOR_LINE_RE = re.compile(r"^@anchor:\s+(url|gist):([^\s|]+)(?:\s*\|.*)?\s*$")
READWISE_FIELD_RE = re.compile(r"\|\s*readwise:\s*[^\s|]+\s*(?=\||$)")

URL_CATEGORIES = {
    "github_code": re.compile(r"github\.com/.+/blob/"),
    "github_issue": re.compile(r"github\.com/.+/(issues|discussions|pull)/"),
    "github_repo": re.compile(r"github\.com/[^/]+/[^/]+/?$"),
    "docs": re.compile(r"docs\.ray\.io"),
    "deepwiki": re.compile(r"deepwiki\.com"),
    "wikipedia": re.compile(r"wikipedia\.org"),
    "pdf": re.compile(r"\.(pdf|PDF)($|\?)"),
    "article": re.compile(r".*"),  # catch-all, must be last
}


def categorize_url(url: str) -> str:
    """Return the category name for a URL. Categories are checked in order;
    'article' is the catch-all."""
    for name, pattern in URL_CATEGORIES.items():
        if name == "article":
            continue
        if pattern.search(url):
            return name
    return "article"


def find_anchors_missing_readwise(
    note_path: Path | None = None,
) -> list[dict]:
    """Return anchor locations and exact source lines lacking a Readwise ID."""
    if note_path:
        files = [note_path]
    else:
        # rglob: wiki entries live in domain subdirectories, not at the top level.
        files = sorted(WIKI_DIR.rglob("*.md"))

    results = []
    for fpath in files:
        if fpath.is_symlink():
            raise ValueError(f"symlink note refused: {fpath}; select its canonical target explicitly")
        lines = fpath.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            m = ANCHOR_LINE_RE.match(line)
            if not m:
                continue
            if READWISE_FIELD_RE.search(line):
                continue
            url = m.group(2)
            results.append(
                {
                    "path": fpath,
                    "line_no": i,
                    "line": line,
                    "url": url,
                    "anchor_type": m.group(1),
                    "category": categorize_url(url),
                }
            )
    return results


def _document_id(data: object) -> str | None:
    value = (data.get("document_id") or data.get("id")) if isinstance(data, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"[^\s|]+", value) else None


def search_readwise_for_url(url: str) -> str | None:
    """Check if a URL is already saved in Readwise.
    Returns the document ID if found, None otherwise.

    Uses reader-search-documents to fetch candidates, then matches the URL
    against each candidate's source_url/url fields, because search alone
    uses hybrid/semantic matching and may return unrelated results.
    """
    try:
        result = subprocess.run(
            [
                "readwise",
                "reader-search-documents",
                "--query", url,
                "--limit", "5",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        docs = json.loads(result.stdout)
        if not isinstance(docs, list):
            return None
        for doc in docs:
            if isinstance(doc, dict) and url in (doc.get("source_url"), doc.get("url")):
                if doc_id := _document_id(doc):
                    return doc_id
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def save_to_readwise(url: str) -> str | None:
    """Save a URL to Readwise with the anchor-evidence tag.
    Returns the document ID on success, None on failure."""
    try:
        result = subprocess.run(
            [
                "readwise",
                "reader-create-document",
                "--url", url,
                "--tags", "anchor-evidence",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            sys.stderr.write(
                f"  readwise save failed for {url}: {result.stderr.strip()}\n"
            )
            return None
        data = json.loads(result.stdout)
        doc_id = _document_id(data)
        if doc_id:
            return doc_id
        sys.stderr.write(
            f"  readwise save returned no ID for {url}: {result.stdout.strip()}\n"
        )
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"  readwise save error for {url}: {e}\n")
        return None


def backfill_readwise_id(path: Path, line_no: int, doc_id: str, expected_line: str) -> bool:
    """Backfill only the discovered anchor, preserving other bytes and permissions."""
    try:
        if path.is_symlink():
            raise ValueError("symlink note refused; select its canonical target explicitly")
        with path.open(encoding="utf-8", newline="") as handle:
            lines = handle.read().splitlines(keepends=True)
        idx = line_no - 1
        if not 0 <= idx < len(lines) or lines[idx].rstrip("\r\n") != expected_line:
            raise ValueError(f"anchor changed at line {line_no}; rediscover before retrying")
        if not ANCHOR_LINE_RE.fullmatch(expected_line) or not _document_id({"id": doc_id}):
            raise ValueError("invalid anchor or document ID")
        ending = lines[idx][len(expected_line):]
        lines[idx] = f"{expected_line} | readwise: {doc_id}{ending}"
        atomic_write(path, "".join(lines), newline="")
        return True
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"  backfill failed for {path}: {exc}\n")
        return False


def report_categories(anchors: list[dict]) -> str:
    """Group missing-readwise anchors by category and return a report."""
    by_cat: dict[str, list[str]] = {}
    seen_urls: set[str] = set()
    for a in anchors:
        url = a["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        cat = a["category"]
        by_cat.setdefault(cat, []).append(url)

    lines = [f"Anchor snapshot report: {len(seen_urls)} unique URLs missing readwise:", ""]
    for cat in URL_CATEGORIES:
        urls = by_cat.get(cat, [])
        if not urls:
            continue
        lines.append(f"  {cat} ({len(urls)}):")
        for url in sorted(urls):
            lines.append(f"    {url}")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/snapshot_anchors.py",
        description="Save wiki url:/gist: anchors to Readwise and backfill document IDs.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually save to Readwise and write IDs back to wiki files. "
             "Without this flag, only a dry run is performed.",
    )
    parser.add_argument(
        "--note",
        type=Path,
        default=None,
        help="Process a single wiki entry instead of all entries.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Show URLs grouped by category, then exit.",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=1.5,
        help="Seconds to wait between Readwise API calls (default: 1.5).",
    )
    args = parser.parse_args(argv)

    if not WIKI_DIR.is_dir():
        sys.stderr.write(f"error: {WIKI_DIR} not found. Run from the repo root.\n")
        return 2

    try:
        anchors = find_anchors_missing_readwise(args.note)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    if not anchors:
        print("All url:/gist: anchors already have readwise: IDs. Nothing to do.")
        return 0

    if args.report:
        print(report_categories(anchors))
        return 0

    url_to_anchors: dict[str, list[dict]] = {}
    for a in anchors:
        url_to_anchors.setdefault(a["url"], []).append(a)

    if not url_to_anchors:
        print("All remaining anchors are in skipped categories. Nothing to do.")
        return 0

    print(f"Found {len(url_to_anchors)} unique URLs to process "
          f"({sum(len(v) for v in url_to_anchors.values())} anchor lines total)")

    if not args.apply:
        print("\nDry run (pass --apply to save to Readwise and backfill IDs):\n")
        for url, anchor_list in sorted(url_to_anchors.items()):
            cat = anchor_list[0]["category"]
            locations = ", ".join(
                f"{a['path'].name}:{a['line_no']}" for a in anchor_list
            )
            print(f"  [{cat:15s}] {url}")
            print(f"                    in: {locations}")
        return 0

    saved = 0
    failed = 0
    already_in_readwise = 0

    for url, anchor_list in sorted(url_to_anchors.items()):
        cat = anchor_list[0]["category"]
        print(f"  [{cat:15s}] {url} ... ", end="", flush=True)

        doc_id = search_readwise_for_url(url)
        if doc_id:
            print(f"already saved (ID: {doc_id})")
            already_in_readwise += 1
        else:
            time.sleep(args.rate_limit)
            doc_id = save_to_readwise(url)
            if not doc_id:
                print("FAILED")
                failed += 1
                continue
            print(f"saved (ID: {doc_id})")
            saved += 1

        for a in anchor_list:
            ok = backfill_readwise_id(a["path"], a["line_no"], doc_id, a["line"])
            if ok:
                print(f"    backfilled {a['path'].name}:{a['line_no']}")
            else:
                failed += 1
                print(f"    FAILED to backfill {a['path'].name}:{a['line_no']}")

    print(f"\nDone: {saved} newly saved, {already_in_readwise} already in Readwise, "
          f"{failed} failed")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
