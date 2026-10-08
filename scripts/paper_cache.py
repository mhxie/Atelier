#!/usr/bin/env python3
"""Build and reuse a vault-local text cache for a research paper PDF.

The source PDF remains in the L3 papers or preprints tier. Derived text lives
in the L1 cache tier so parallel readers can share it without putting scratch
artifacts in the Atelier repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _paths import atomic_write, fmt, tier


METADATA_VERSION = 1


def slugify(value: str) -> str:
    """Return a stable filesystem slug while retaining Unicode letters."""
    parts: list[str] = []
    pending_separator = False
    for char in value.strip().lower():
        if char.isalnum() or char in "._-":
            if pending_separator and parts and parts[-1] != "-":
                parts.append("-")
            parts.append(char)
            pending_separator = False
        else:
            pending_separator = True
    slug = re.sub(r"-+", "-", "".join(parts)).strip("._-")
    if not slug or slug in {".", ".."}:
        raise ValueError("paper filename does not produce a usable cache slug")
    return slug


def source_metadata(pdf: Path) -> dict[str, object]:
    stat = pdf.stat()
    return {
        "version": METADATA_VERSION,
        "source": fmt(pdf),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "extractor": "pdftotext -layout",
    }


def load_metadata(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def require_paper_source(pdf: Path) -> None:
    roots = (tier("papers").resolve(), tier("preprints").resolve())
    if not any(pdf.is_relative_to(root) for root in roots):
        raise ValueError(
            "source PDF must be stored under <paths.papers>/ or "
            "<paths.preprints>/ before extraction"
        )


def extract(pdf: Path, output: Path) -> None:
    executable = shutil.which("pdftotext")
    if not executable:
        raise RuntimeError("pdftotext is required; install Poppler before caching papers")

    output.parent.mkdir(parents=True, exist_ok=True)
    handle, raw_temp = tempfile.mkstemp(prefix=".paper.txt.", dir=output.parent)
    os.close(handle)
    temp_path = Path(raw_temp)
    temp_path.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [executable, "-layout", str(pdf), str(temp_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"pdftotext failed: {detail}")
        if not temp_path.is_file():
            raise RuntimeError("pdftotext completed without producing paper.txt")
        with temp_path.open("r", encoding="utf-8", errors="replace") as stream:
            if not any(chunk.strip() for chunk in iter(lambda: stream.read(65536), "")):
                raise RuntimeError(
                    "pdftotext produced no readable text; use OCR or page inspection"
                )
        temp_path.replace(output)
    finally:
        temp_path.unlink(missing_ok=True)


def build_cache(pdf: Path, *, slug: str | None, force: bool) -> dict[str, object]:
    pdf = pdf.expanduser().resolve()
    if not pdf.is_file():
        raise ValueError(f"paper PDF not found: {pdf}")
    if pdf.suffix.lower() != ".pdf":
        raise ValueError("paper source must have a .pdf extension")
    require_paper_source(pdf)

    cache_slug = slugify(slug if slug is not None else pdf.stem)
    cache_dir = tier("cache") / cache_slug
    text_path = cache_dir / "paper.txt"
    index_path = cache_dir / "index.md"
    metadata_path = cache_dir / "source.json"
    expected = source_metadata(pdf)
    existing = load_metadata(metadata_path)
    if cache_dir.exists() and existing is None and any(cache_dir.iterdir()):
        raise ValueError(
            f"cache directory has no valid source metadata: {fmt(cache_dir)}; "
            "use --slug for a distinct cache"
        )
    if existing is not None and existing.get("source") != expected["source"]:
        raise ValueError(
            f"cache slug already belongs to another PDF: {fmt(cache_dir)}; "
            "use --slug for a distinct cache"
        )

    if (
        not force
        and text_path.is_file()
        and index_path.is_file()
        and existing == expected
    ):
        status = "cached"
    else:
        extract(pdf, text_path)
        index = (
            "---\n"
            f"source_pdf: {json.dumps(expected['source'], ensure_ascii=False)}\n"
            "derived: true\n"
            "cache_tier: L1\n"
            "extractor: pdftotext-layout\n"
            "---\n\n"
            "## Contents\n\n"
            "- `paper.txt`: layout-preserving text extracted from the source PDF.\n"
            "- `source.json`: source signature used to detect a stale extraction.\n"
            "- `pages/`: optional page renders explicitly retained for later reading.\n"
        )
        atomic_write(index_path, index)
        atomic_write(
            metadata_path,
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
        )
        status = "extracted"

    return {
        "status": status,
        "source": fmt(pdf),
        "cache_path": fmt(cache_dir),
        "text_path": fmt(text_path),
        "index_path": fmt(index_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cache a paper PDF as reusable text under <paths.cache>/."
    )
    parser.add_argument(
        "pdf",
        type=Path,
        help="PDF under <paths.papers>/ or <paths.preprints>/",
    )
    parser.add_argument("--slug", help="Override the cache directory slug")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even when the cache is fresh",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured result JSON")
    args = parser.parse_args()

    try:
        payload = build_cache(args.pdf, slug=args.slug, force=args.force)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"paper_cache: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload["cache_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
