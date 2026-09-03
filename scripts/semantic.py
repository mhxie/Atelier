#!/usr/bin/env python3
"""
semantic.py: local semantic search interface for the zk/ vault.

STUB MODE:
    Lexical fallback using tokenized substring matching over the local
    Markdown corpus. Returns path, score, matched-token rows.
    Active when the index directory does NOT exist.

REAL MODE:
    Embedding-backed search using pluggable Embedder + Store backends.
    Day-one stack: BGE-M3 (sentence-transformers) + LanceDB.
    Active when a lance directory exists. Reads prefer
    ~/.cache/atelier/lance/; if absent, fall back to legacy
    ~/.cache/reflectl/lance/ (existing installs migrate on next index).

The CLI contract is encoder-agnostic and frozen. Swapping the backend from
stub to real will NOT change caller code in command files or agents.

See also:
    sources/semantic.md                          (teaching doc, stable)
    scripts/semantic_backends.py                 (backend implementations)

Usage:
    scripts/semantic.py query "<text>" [OPTIONS]
    scripts/semantic.py status [--format text|json]
    scripts/semantic.py corpus [--format text|json]
    scripts/semantic.py index [--rebuild | --if-stale]
    scripts/semantic.py --help

Stdlib only in stub mode. Real mode requires: sentence-transformers, lancedb.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from _paths import parse_iso_date, vault_root  # type: ignore[import-not-found]  # noqa: E402
from semantic_corpus import (  # type: ignore[import-not-found]  # noqa: E402
    ACTIVE_SCOPE,
    corpus_metadata_fingerprint,  # noqa: F401  (re-export; harness_smoke calls semantic.corpus_metadata_fingerprint)
    ALL_SCOPE,
    POLICY_FINGERPRINT,
    RAW_LOCATOR_REPRESENTATION,
    RAW_SCOPE,
    VALID_SCOPES,
    CorpusRecord,
    audit_corpus,
    build_raw_locator_records,
    iter_corpus_records,
    iter_file_decisions,
    path_prefix_matches,
    physical_manifest_fingerprint,
)

# Lance index is machine-local (rebuild is ~7s on MPS, not worth syncing binaries).
# Per-embedder subdirs keep indices from different models isolated by dimension
# and vocabulary; the default (bge-m3) keeps the legacy path for backwards-
# compat with installs that already have a built index.
_EMBEDDER_SUBDIR = {
    "bge-m3": "lance",
    "qwen3-0.6b": "lance-qwen3-0.6b",
    "qwen3-4b": "lance-qwen3-4b",
    "qwen3-8b": "lance-qwen3-8b",
}

# Mirror of make_embedder()'s alias map in semantic_backends.py. Keep these
# two tables in sync: if make_embedder accepts a short alias, this map must
# normalize it to the canonical key used in _EMBEDDER_SUBDIR or the lance
# dir will mismatch the embedding model and reads will return garbage.
_EMBEDDER_ALIAS = {
    "bgem3": "bge-m3",
    "": "bge-m3",
    "qwen3": "qwen3-0.6b",
    "qwen-0.6b": "qwen3-0.6b",
    "qwen-4b": "qwen3-4b",
    "qwen-8b": "qwen3-8b",
}


def _active_embedder_key() -> str:
    import os

    raw = (os.environ.get("SEMANTIC_EMBEDDER") or "bge-m3").lower()
    return _EMBEDDER_ALIAS.get(raw, raw)


def _lance_root_for(key: str) -> Path:
    return Path.home() / ".cache" / "atelier" / _EMBEDDER_SUBDIR.get(key, "lance")


_LANCE_NEW = _lance_root_for(_active_embedder_key())
# Reads fall back to the pre-rename location so existing installs keep
# semantic search without a forced rebuild. Writes always go to _LANCE_NEW.
_LANCE_OLD = Path.home() / ".cache" / "reflectl" / "lance"


def _resolve_lance_dir(prefer_new: bool = False) -> Path:
    """Resolve the active lance index directory for the active embedder."""
    if prefer_new:
        return _LANCE_NEW
    if _LANCE_NEW.exists():
        return _LANCE_NEW
    if _active_embedder_key() == "bge-m3" and _LANCE_OLD.exists():
        return _LANCE_OLD
    return _LANCE_NEW


# Module-level binding kept for backwards-compat with callers that read it
# directly (e.g. db_path string interpolation). For read-side code paths.
LANCE_DIR = _resolve_lance_dir()
# Default scan root is the vault ($OV). Resolved lazily at use sites
# via vault_root() so `--help` and other no-vault probes work without
# $OV set; only commands that actually walk the vault (query, index)
# require $OV.
DEFAULT_PATH: str | None = None


@dataclass
class QueryHit:
    path: str
    score: float
    chunk_id: int = 0
    chunk_text: str = ""
    tier: str = ""
    mtime: float = 0.0
    source: str = "local"
    scope: str = ACTIVE_SCOPE
    representation: str = "authored"
    matched_tokens: tuple[str, ...] = ()


def in_real_mode() -> bool:
    """Sentinel check: real mode is active iff the lance directory exists."""
    return LANCE_DIR.exists()


def mode_label() -> str:
    return "real" if in_real_mode() else "stub"


def warn(msg: str) -> None:
    """Emit a warning to stderr. Never to stdout (which carries results)."""
    print(f"[semantic.py] {msg}", file=sys.stderr)


@contextlib.contextmanager
def _exclusive_index_lock() -> Iterator[bool]:
    """Prevent scheduled and interactive writers from mutating one index together."""
    try:
        import fcntl
    except ImportError:
        yield True
        return

    lock_dir = Path.home() / ".cache" / "atelier"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"semantic-{_active_embedder_key()}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _corpus_snapshot(
    vault: Path,
) -> tuple[tuple[Any, ...], list[Path], tuple[CorpusRecord, ...]]:
    """Classify one consistent physical snapshot and derive locator records."""
    decisions = tuple(iter_file_decisions(vault))
    physical_files = [
        item.absolute_path
        for item in decisions
        if item.included and item.absolute_path is not None
    ]
    locators = build_raw_locator_records(vault, files=decisions)
    return decisions, physical_files, locators


def _current_corpus_manifest(
    vault: Path,
) -> tuple[dict[str, dict[str, Any]], int, tuple[CorpusRecord, ...], list[str]]:
    decisions, _, locators = _corpus_snapshot(vault)
    manifest: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    physical_count = 0
    for item in decisions:
        if item.included:
            physical_count += 1
            assert item.scope is not None and item.representation is not None
            manifest[item.relative_path] = {
                "mtime": item.mtime,
                "manifest_fingerprint": physical_manifest_fingerprint(
                    item.scope,
                    item.representation,
                    item.mtime,
                ),
            }
        elif item.exclusion_reason == "unreadable":
            unreadable.append(item.relative_path)
    for record in locators:
        manifest[record.path] = {
            "mtime": record.mtime,
            "manifest_fingerprint": record.manifest_fingerprint,
        }
    return manifest, physical_count, locators, sorted(unreadable)


def _freshness_from_manifest(
    current: dict[str, dict[str, Any]],
    indexed: dict[str, dict[str, Any]],
    *,
    physical_count: int,
    unreadable: Sequence[str] = (),
) -> dict[str, Any]:
    """Compare physical and generated corpus records with the stored manifest."""
    current_paths = set(current)
    indexed_paths = set(indexed)
    new_paths = sorted(current_paths - indexed_paths)
    removed_paths = sorted(indexed_paths - current_paths)
    modified_paths: list[str] = []
    for path in sorted(current_paths & indexed_paths):
        current_row = current[path]
        indexed_row = indexed[path]
        mtime_changed = (
            abs(
                float(current_row.get("mtime", 0.0))
                - float(indexed_row.get("mtime", 0.0))
            )
            > 1.0
        )
        fingerprint = str(current_row.get("manifest_fingerprint", "") or "")
        indexed_fingerprint = str(indexed_row.get("manifest_fingerprint", "") or "")
        fingerprint_changed = bool(fingerprint) and fingerprint != indexed_fingerprint
        if mtime_changed or fingerprint_changed:
            modified_paths.append(path)

    unreadable_paths = sorted(unreadable)
    return {
        "fresh": not (new_paths or modified_paths or removed_paths or unreadable_paths),
        "current_files": physical_count,
        "current_records": len(current),
        "indexed_files": len(indexed),
        "indexed_records": len(indexed),
        "new": len(new_paths),
        "modified": len(modified_paths),
        "removed": len(removed_paths),
        "unreadable": len(unreadable_paths),
        "samples": {
            "new": new_paths[:20],
            "modified": modified_paths[:20],
            "removed": removed_paths[:20],
            "unreadable": unreadable_paths[:20],
        },
    }


def inspect_index_freshness(
    *,
    vault: Optional[Path] = None,
    index_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Inspect index drift without loading the embedding model."""
    from semantic_backends import (
        LanceStore,
        read_lance_index_manifest,
        read_lance_index_schema_columns,
    )

    active_vault = vault or vault_root()
    active_index = index_dir or LANCE_DIR
    current_manifest, physical_count, locators, unreadable = _current_corpus_manifest(
        active_vault
    )

    indexed_manifest: dict[str, dict[str, Any]] = {}
    index_present = False
    schema_current = False
    error: Optional[str] = None
    if active_index.exists():
        try:
            stored = read_lance_index_manifest(str(active_index))
            if stored is not None:
                indexed_manifest = stored
                index_present = True
                columns = read_lance_index_schema_columns(str(active_index)) or set()
                schema_current = LanceStore.CORPUS_METADATA_COLUMNS.issubset(columns)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    result = _freshness_from_manifest(
        current_manifest,
        indexed_manifest,
        physical_count=physical_count,
        unreadable=unreadable,
    )
    result.update(
        {
            "index_path": str(active_index),
            "index_present": index_present,
            "schema_current": schema_current,
            "policy_current": _manifest_uses_current_policy(indexed_manifest),
            "corpus_policy": POLICY_FINGERPRINT,
            "raw_locator_records": len(locators),
            "error": error,
        }
    )
    if (
        not index_present
        or not schema_current
        or not result["policy_current"]
        or error is not None
    ):
        result["fresh"] = False
    return result


def _freshness_summary(result: dict[str, Any]) -> str:
    state = "fresh" if result["fresh"] else "stale"
    summary = (
        f"{state}: current_files={result['current_files']}, "
        f"current_records={result.get('current_records', result['current_files'])}, "
        f"indexed_records={result.get('indexed_records', result['indexed_files'])}, "
        f"new={result['new']}, "
        f"modified={result['modified']}, removed={result['removed']}, "
        f"unreadable={result['unreadable']}"
    )
    if result.get("schema_current") is False:
        summary += ", schema=migration-required"
    if result.get("policy_current") is False:
        summary += ", policy=migration-required"
    if result.get("error"):
        summary += f", error={result['error']}"
    return summary


# ---------------------------------------------------------------------------
# Stub mode (lexical fallback) -- unchanged from original
# ---------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r"[\s,./;:!?()\[\]{}\"'`\u2014\u2013-]+")


def tokenize(query: str) -> List[str]:
    return [t.lower() for t in _TOKEN_SPLIT.split(query.strip()) if t]


def parse_date(s: Optional[str], flag_name: str) -> Optional[datetime]:
    if s is None:
        return None
    parsed = parse_iso_date(s) if len(s.strip()) == 10 else None
    if parsed is None:
        warn(f"invalid {flag_name} value (expected YYYY-MM-DD): {s}")
        sys.exit(2)
    return datetime(parsed.year, parsed.month, parsed.day)


def lexical_score_text(text: str, tokens: List[str]) -> Tuple[float, List[str]]:
    text = text.lower()
    matched: List[str] = []
    total = 0
    for tok in tokens:
        n = text.count(tok)
        if n > 0:
            matched.append(tok)
            total += n
    if not matched:
        return 0.0, []
    return min(1.0, total / 10.0), matched


def _derive_hit_tier(path: str, representation: str) -> str:
    if representation in {"raw_text", RAW_LOCATOR_REPRESENTATION}:
        return "L1"
    top = path.split("/", 1)[0]
    try:
        from _paths import tier_segments, wiki_dirs

        if any(
            str(directory.relative_to(vault_root())).split("/", 1)[0] == top
            for directory in wiki_dirs()
        ):
            return "L4"
        segments = tier_segments()
        if top in {
            segments.get("papers", "papers").split("/", 1)[0],
            segments.get("preprints", "preprints").split("/", 1)[0],
        }:
            return "L3"
        if top in {
            segments.get("cache", "cache").split("/", 1)[0],
            segments.get("inbox", "inbox").split("/", 1)[0],
        }:
            return "L1"
    except Exception:
        if top == "wiki":
            return "L4"
        if top in {"papers", "preprints"}:
            return "L3"
        if top in {"cache", "inbox"}:
            return "L1"
    return "L2"


def _normalize_path_prefix(value: str, vault: Path) -> Optional[str]:
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(vault).as_posix()
        except ValueError:
            warn(f"--path {value} is outside the vault ({vault}); ignoring")
            return None
    return value.strip("/")


def _path_matches(path: str, prefixes: Sequence[str]) -> bool:
    return any(path_prefix_matches(path, prefix) for prefix in prefixes)


def _hit_sort_key(
    hit: QueryHit,
    requested_scope: str,
) -> tuple[int, float, str, int]:
    """Keep generated locator cards behind authored results in active search."""
    locator_tail = int(
        requested_scope == ACTIVE_SCOPE
        and hit.representation == RAW_LOCATOR_REPRESENTATION
    )
    return (locator_tail, -hit.score, hit.path, hit.chunk_id)


def _calibrate_active_locator_tail(
    hits: Sequence[QueryHit],
    requested_scope: str,
) -> list[QueryHit]:
    """Make displayed scores agree with the active authored-first ordering."""
    if requested_scope != ACTIVE_SCOPE:
        return list(hits)
    calibrated: list[QueryHit] = []
    for hit in hits:
        if (
            calibrated
            and hit.representation == RAW_LOCATOR_REPRESENTATION
            and hit.score >= calibrated[-1].score
        ):
            hit = replace(
                hit,
                score=round(max(0.0, calibrated[-1].score - 0.0001), 4),
            )
        calibrated.append(hit)
    return calibrated


def _collapse_hits(
    hits: Sequence[QueryHit],
    *,
    top: int,
    requested_scope: str,
) -> list[QueryHit]:
    """Keep the best chunk per path and cap active raw-locator competition."""
    if top <= 0:
        return []
    ordered = sorted(hits, key=lambda hit: _hit_sort_key(hit, requested_scope))
    unique: list[QueryHit] = []
    seen: set[tuple[str, str]] = set()
    locator_count = 0
    for hit in ordered:
        key = (hit.source, hit.path)
        if key in seen:
            continue
        if (
            requested_scope == ACTIVE_SCOPE
            and hit.representation == RAW_LOCATOR_REPRESENTATION
        ):
            if locator_count >= 2:
                continue
            locator_count += 1
        seen.add(key)
        unique.append(hit)
        if len(unique) >= top:
            break
    return _calibrate_active_locator_tail(unique, requested_scope)


def _rank_hits(
    hits: Sequence[QueryHit],
    *,
    top: int,
    requested_scope: str,
) -> list[QueryHit]:
    """Preserve chunk ranking while bounding raw cards in active search."""
    if top <= 0:
        return []
    selected: list[QueryHit] = []
    locator_count = 0
    for hit in sorted(hits, key=lambda hit: _hit_sort_key(hit, requested_scope)):
        if (
            requested_scope == ACTIVE_SCOPE
            and hit.representation == RAW_LOCATOR_REPRESENTATION
        ):
            if locator_count >= 2:
                continue
            locator_count += 1
        selected.append(hit)
        if len(selected) >= top:
            break
    return _calibrate_active_locator_tail(selected, requested_scope)


def _backfill_locator_hit(
    selected: Sequence[QueryHit],
    supplements: Sequence[QueryHit],
    *,
    top: int,
    requested_scope: str,
    collapse: bool,
) -> list[QueryHit]:
    """Reserve at most one tail slot for an exact raw-locator navigation hit.

    The locator never competes on a dense, tier, or cross-encoder score scale.
    In active search, one authored result is always retained ahead of it.
    """
    if top <= 0:
        return []
    chosen = (
        _collapse_hits(supplements, top=1, requested_scope=requested_scope)
        if collapse
        else _rank_hits(supplements, top=1, requested_scope=requested_scope)
    )
    base = list(selected[:top])
    if not chosen:
        return base

    locator = chosen[0]
    identity = (
        (locator.source, locator.path)
        if collapse
        else (locator.source, locator.path, locator.chunk_id)
    )
    existing = {
        (hit.source, hit.path) if collapse else (hit.source, hit.path, hit.chunk_id)
        for hit in base
    }
    if identity in existing:
        return base
    if (
        requested_scope == ACTIVE_SCOPE
        and top == 1
        and any(hit.representation != RAW_LOCATOR_REPRESENTATION for hit in base)
    ):
        return base

    if requested_scope == ACTIVE_SCOPE:
        non_locators = [
            hit for hit in base if hit.representation != RAW_LOCATOR_REPRESENTATION
        ]
        existing_locators = [
            hit for hit in base if hit.representation == RAW_LOCATOR_REPRESENTATION
        ]
        base = non_locators[: max(0, top - 1)]
        remaining = max(0, top - 1 - len(base))
        # One existing dense locator plus the exact backfill stays within the
        # active two-card cap when authored results do not fill the page.
        base.extend(existing_locators[: min(1, remaining)])
    else:
        base = base[: max(0, top - 1)]

    score = max(0.0, min(1.0, locator.score))
    if base:
        score = min(score, max(0.0, base[-1].score - 0.0001))
    return [*base, replace(locator, score=round(score, 4))]


def _locator_backfill_enabled(scope: str) -> bool:
    """Raw locator navigation is orthogonal to dense or hybrid retrieval."""
    return scope in {ACTIVE_SCOPE, RAW_SCOPE}


def _local_candidate_count(
    top: int,
    *,
    cross_encoder: bool,
    collapse: bool,
) -> int:
    """Return the local candidate pool needed by the selected query mode."""
    if top <= 0:
        return 0
    if cross_encoder:
        return max(top, 30)
    if collapse:
        return max(top * 6, 50)
    return top


_CAPSULE_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_CAPSULE_LIMIT = 600


def _capsule(hit: QueryHit, requested_scope: str) -> dict[str, Any]:
    text = hit.chunk_text.strip()
    heading_match = _CAPSULE_HEADING.search(text)
    heading = (
        heading_match.group(1).strip().rstrip("#").strip()
        if heading_match
        else Path(hit.path).stem
    )
    truncated = len(text) > _CAPSULE_LIMIT
    if truncated:
        marker = "\n[truncated]"
        snippet = text[: _CAPSULE_LIMIT - len(marker)].rstrip() + marker
    else:
        snippet = text
    result_scope = hit.scope
    if hit.source == "local" and requested_scope != ALL_SCOPE:
        result_scope = requested_scope
    return {
        "path": hit.path,
        "score": round(hit.score, 4),
        "source": hit.source,
        "tier": hit.tier,
        "scope": result_scope,
        "representation": hit.representation,
        "chunk_id": hit.chunk_id,
        "heading": heading,
        "snippet": snippet,
        "truncated": truncated,
    }


def _emit_hits(
    hits: Sequence[QueryHit],
    *,
    output_format: str,
    context: bool,
    requested_scope: str,
    include_source: bool,
) -> None:
    if output_format == "json":
        if context:
            payload = [_capsule(hit, requested_scope) for hit in hits]
        else:
            payload = []
            for hit in hits:
                row = {
                    "path": hit.path,
                    "score": round(hit.score, 4),
                    "matched_tokens": list(hit.matched_tokens),
                }
                if include_source:
                    row["source"] = hit.source
                payload.append(row)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    for hit in hits:
        third = ",".join(hit.matched_tokens) if hit.matched_tokens else hit.source
        print(f"{hit.path}\t{hit.score:.3f}\t{third}")


def stub_query(args: argparse.Namespace) -> int:
    warn("stub mode: lexical fallback, results are NOT semantic")

    tokens = tokenize(args.query)
    if not tokens:
        warn("query tokenized to empty; no results")
        return 0

    vault = vault_root()
    prefixes: list[str] = []
    for raw_path in args.path or []:
        prefix = _normalize_path_prefix(raw_path, vault)
        if prefix is not None:
            prefixes.append(prefix)
    if args.path and not prefixes:
        warn("no usable --path filters remain; no results")
        return 0
    after = parse_date(args.after, "--after")
    before = parse_date(args.before, "--before")

    if args.lang != "auto":
        warn(f"--lang {args.lang} is a no-op in stub mode")
    if args.sources != "local":
        warn("stub mode is local-only; external sources are ignored")

    results: list[QueryHit] = []
    for record in iter_corpus_records(vault, scope=args.scope):
        if prefixes and not _path_matches(record.path, prefixes):
            continue
        modified = datetime.fromtimestamp(record.mtime)
        if after and modified < after:
            continue
        if before and modified > before:
            continue
        score, matched = lexical_score_text(
            f"{record.path}\n{record.text}",
            tokens,
        )
        if score > 0:
            results.append(
                QueryHit(
                    path=record.path,
                    score=score,
                    chunk_text=record.text,
                    tier=_derive_hit_tier(record.path, record.representation),
                    mtime=record.mtime,
                    source="local",
                    scope=record.scope,
                    representation=record.representation,
                    matched_tokens=tuple(matched),
                )
            )

    selected = (
        _collapse_hits(
            results,
            top=args.top,
            requested_scope=args.scope,
        )
        if args.context
        else _rank_hits(results, top=args.top, requested_scope=args.scope)
    )
    _emit_hits(
        selected,
        output_format=args.format,
        context=args.context,
        requested_scope=args.scope,
        include_source=False,
    )
    return 0


# ---------------------------------------------------------------------------
# Real mode (embedding-backed search)
# ---------------------------------------------------------------------------


def _load_trust_scores() -> dict:
    """Load wiki trust scores from trust.py. Returns {relative_path: score}."""
    try:
        from trust import load_wiki, score_notes
        from datetime import date as _date

        notes = load_wiki(as_of=_date.today())
        _, note_scores = score_notes(notes, as_of=_date.today())
        # trust.py keys by absolute path; the index stores vault-relative
        # paths. Relativize or the reranker's trust bonus never matches.
        vault = vault_root()
        scores: dict = {}
        for p, s in note_scores.items():
            try:
                scores[str(Path(p).relative_to(vault))] = s
            except ValueError:
                scores[str(p)] = s
        return scores
    except Exception:
        return {}


def _build_retriever(with_reranker: bool = True, hybrid: bool = False):
    """Lazy-import and construct the Retriever with the configured backends.

    `hybrid=True` wraps the dense Retriever in a HybridRetriever that fuses
    BM25 (sparse) results with dense cosine via Reciprocal Rank Fusion.
    """
    from semantic_backends import (
        LanceStore,
        Retriever,
        TierRecencyReranker,
        make_embedder,
    )

    embedder = make_embedder()
    warn(
        f"embedder: {embedder.model_name()} (device: {embedder._device}, dim: {embedder.dimension()}, max_tokens: {embedder._max_tokens})"
    )

    store = LanceStore(
        db_path=str(LANCE_DIR),
        embedding_dim=embedder.dimension(),
        model_name=embedder.model_name(),
    )

    reranker = None
    if with_reranker:
        trust = _load_trust_scores()
        if trust:
            warn(f"loaded trust scores for {len(trust)} wiki entries")
            indexed = store.get_indexed_mtimes()
            if indexed and not (trust.keys() & indexed.keys()):
                warn(
                    "warning: trust keys match no indexed paths; "
                    "trust reranking is a no-op (key format drift?)"
                )
        reranker = TierRecencyReranker(trust_scores=trust)

    retriever = Retriever(embedder=embedder, store=store, reranker=reranker)
    if hybrid:
        from semantic_backends import HybridRetriever

        warn("hybrid: BM25 + dense (RRF)")
        retriever = HybridRetriever(base=retriever)
    return retriever


def real_query(args: argparse.Namespace) -> int:
    warn("real mode: embedding-backed semantic search")

    if not args.query.strip():
        warn("empty query; no results")
        return 0

    # Resolve $OV early; fail fast before loading the embedder.
    default_path = str(vault_root())

    sources = {source.strip() for source in args.sources.split(",") if source.strip()}
    unknown_sources = sources - {"local", "readwise"}
    if unknown_sources:
        warn(f"unknown --sources value(s): {', '.join(sorted(unknown_sources))}")
        return 2
    if args.top <= 0:
        _emit_hits(
            [],
            output_format=args.format,
            context=args.context,
            requested_scope=args.scope,
            include_source=True,
        )
        return 0
    retriever = _build_retriever(hybrid=getattr(args, "hybrid", False))

    # Optional cross-encoder rerank over the merged top-N candidate set.
    cross_encoder = None
    if (
        getattr(args, "rerank", "auto") == "ce"
        or getattr(args, "rerank", "auto") == "auto"
    ):
        # In `auto` mode the cross-encoder is opt-in via env to avoid a model
        # download on first use; explicit `ce` always loads it.
        import os

        if args.rerank == "ce" or os.environ.get("SEMANTIC_RERANK_CE") == "1":
            from semantic_backends import CrossEncoderReranker

            warn("cross-encoder rerank: BAAI/bge-reranker-v2-m3")
            cross_encoder = CrossEncoderReranker()

    # Build filters from CLI args
    filters = {"scope": args.scope}
    paths = args.path or [default_path]
    if paths != [default_path]:
        # The index stores vault-relative paths, so prefix filters must be
        # vault-relative too. Relativize absolute --path values; reject ones
        # outside the vault loudly instead of silently matching nothing.
        rel_paths = []
        for p in paths:
            normalized = _normalize_path_prefix(p, vault_root())
            if normalized is not None:
                rel_paths.append(normalized)
        if not rel_paths:
            warn("no usable --path filters remain; no results")
            return 0
        filters["path_prefix"] = rel_paths

    after_dt = parse_date(args.after, "--after")
    before_dt = parse_date(args.before, "--before")
    if after_dt:
        filters["mtime_after"] = after_dt.timestamp()
    if before_dt:
        filters["mtime_before"] = before_dt.timestamp()

    t0 = time.time()
    results: list[QueryHit] = []
    locator_supplements: list[QueryHit] = []
    collapse_results = args.context or len(sources) > 1

    # Local search. When the cross-encoder is enabled we pull a wider
    # candidate pool from the dense+hybrid layer so the cross-encoder has
    # enough material to reorder meaningfully.
    if "local" in sources:
        candidate_k = _local_candidate_count(
            args.top,
            cross_encoder=cross_encoder is not None,
            collapse=collapse_results,
        )
        local_results = retriever.query(
            args.query, top_k=candidate_k, filters=filters or None
        )
        if cross_encoder:
            local_results = cross_encoder.rerank(
                args.query, local_results, top_k=candidate_k
            )
        if _locator_backfill_enabled(args.scope):
            from semantic_backends import search_raw_locators_lexical

            locator_results = search_raw_locators_lexical(
                retriever.store,
                args.query,
                top_k=max(args.top, 2),
                filters=filters,
            )
            locator_supplements.extend(
                QueryHit(
                    path=result.path,
                    score=result.score,
                    chunk_id=result.chunk_id,
                    chunk_text=result.chunk_text,
                    tier=result.tier,
                    mtime=result.mtime,
                    source=result.source,
                    scope=result.scope,
                    representation=result.representation,
                )
                for result in locator_results
            )
            if locator_results:
                warn(f"raw locator lexical backfill: {len(locator_results)} candidates")
        results.extend(
            QueryHit(
                path=result.path,
                score=result.score,
                chunk_id=result.chunk_id,
                chunk_text=result.chunk_text,
                tier=result.tier,
                mtime=result.mtime,
                source=result.source,
                scope=result.scope,
                representation=result.representation,
            )
            for result in local_results
        )
        warn(f"local: {len(local_results)} results")

    # Readwise federated search
    if "readwise" in sources:
        from semantic_backends import ReadwiseSearcher

        if ReadwiseSearcher.available():
            rw_results = ReadwiseSearcher.search(args.query, top_k=args.top)
            results.extend(
                QueryHit(
                    path=result.path,
                    score=result.score,
                    chunk_id=result.chunk_id,
                    chunk_text=result.chunk_text,
                    tier=result.tier,
                    mtime=result.mtime,
                    source=result.source,
                    scope=result.scope,
                    representation=result.representation,
                )
                for result in rw_results
            )
            warn(f"readwise: {len(rw_results)} results")
        else:
            warn("readwise: CLI not installed, skipping")

    results = (
        _collapse_hits(
            results,
            top=args.top,
            requested_scope=args.scope,
        )
        if collapse_results
        else _rank_hits(results, top=args.top, requested_scope=args.scope)
    )
    results = _backfill_locator_hit(
        results,
        locator_supplements,
        top=args.top,
        requested_scope=args.scope,
        collapse=collapse_results,
    )

    elapsed = time.time() - t0
    warn(f"total: {len(results)} results in {elapsed:.2f}s")

    _emit_hits(
        results,
        output_format=args.format,
        context=args.context,
        requested_scope=args.scope,
        include_source=True,
    )

    return 0


def _record_manifest(
    decisions: Sequence[Any],
    locators: Sequence[CorpusRecord],
) -> dict[str, dict[str, Any]]:
    manifest = {
        item.relative_path: {
            "mtime": item.mtime,
            "manifest_fingerprint": physical_manifest_fingerprint(
                str(item.scope),
                str(item.representation),
                item.mtime,
            ),
        }
        for item in decisions
        if item.included
    }
    manifest.update(
        {
            record.path: {
                "mtime": record.mtime,
                "manifest_fingerprint": record.manifest_fingerprint,
            }
            for record in locators
        }
    )
    return manifest


def _manifest_changed(
    current: dict[str, Any],
    indexed: dict[str, Any],
) -> bool:
    if abs(float(current.get("mtime", 0.0)) - float(indexed.get("mtime", 0.0))) > 1.0:
        return True
    fingerprint = str(current.get("manifest_fingerprint", "") or "")
    return bool(fingerprint) and fingerprint != str(
        indexed.get("manifest_fingerprint", "") or ""
    )


def _manifest_uses_current_policy(
    manifest: dict[str, dict[str, Any]],
) -> bool:
    """Whether every indexed record declares the current corpus policy."""
    if not manifest:
        return False
    prefix = POLICY_FINGERPRINT + ":"
    return all(
        (fingerprint := str(row.get("manifest_fingerprint", "") or "")).startswith(
            prefix
        )
        and len(fingerprint.split(":", 3)) >= 3
        for row in manifest.values()
    )


def _search_efficiency_report(
    retriever: Any,
    *,
    audit: dict[str, Any],
    update: dict[str, Any],
) -> dict[str, Any]:
    """Measure scope reduction, query latency, deduplication, and capsule size."""
    probes = (
        "current goals and active commitments",
        "technical architecture and system design",
        "health energy and recovery patterns",
    )
    latencies_ms: list[float] = []
    candidate_rows = 0
    unique_rows = 0
    capsule_bytes = 0
    capsule_count = 0

    for query in probes:
        started = time.perf_counter()
        raw_results = retriever.query(
            query,
            top_k=30,
            filters={"scope": ACTIVE_SCOPE},
        )
        from semantic_backends import search_raw_locators_lexical

        locator_results = search_raw_locators_lexical(
            retriever.store,
            query,
            top_k=10,
            filters={"scope": ACTIVE_SCOPE},
        )
        latencies_ms.append((time.perf_counter() - started) * 1000)
        hits = [
            QueryHit(
                path=result.path,
                score=result.score,
                chunk_id=result.chunk_id,
                chunk_text=result.chunk_text,
                tier=result.tier,
                mtime=result.mtime,
                source=result.source,
                scope=result.scope,
                representation=result.representation,
            )
            for result in raw_results
        ]
        locator_hits = [
            QueryHit(
                path=result.path,
                score=result.score,
                chunk_id=result.chunk_id,
                chunk_text=result.chunk_text,
                tier=result.tier,
                mtime=result.mtime,
                source=result.source,
                scope=result.scope,
                representation=result.representation,
            )
            for result in locator_results
        ]
        collapsed = _collapse_hits(
            hits,
            top=10,
            requested_scope=ACTIVE_SCOPE,
        )
        collapsed = _backfill_locator_hit(
            collapsed,
            locator_hits,
            top=10,
            requested_scope=ACTIVE_SCOPE,
            collapse=True,
        )
        candidate_rows += len(hits) + len(locator_hits)
        unique_rows += len(collapsed)
        for hit in collapsed:
            capsule_bytes += len(
                json.dumps(
                    _capsule(hit, ACTIVE_SCOPE),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            capsule_count += 1

    sorted_latency = sorted(latencies_ms)
    active_records = int(audit["by_scope"][ACTIVE_SCOPE]["records"])
    all_records = int(audit["by_scope"][ALL_SCOPE]["records"])
    scope_reduction = 1.0 - active_records / all_records if all_records else 0.0
    duplicate_reduction = 1.0 - unique_rows / candidate_rows if candidate_rows else 0.0
    return {
        "schema": 1,
        "update": update,
        "index": {
            "chunks": retriever.stats().total_chunks,
            "model": retriever.stats().model_name,
        },
        "corpus": {
            "active_records": active_records,
            "all_records": all_records,
            "default_scope_reduction_pct": round(scope_reduction * 100, 2),
            "excluded_files": int(audit["summary"]["excluded_files"]),
            "raw_assets": int(audit["raw"]["assets"]),
            "raw_locator_records": int(audit["raw"]["locator_records"]),
            "readable_raw_files": int(audit["raw"]["readable_files"]),
        },
        "query_probe": {
            "queries": len(probes),
            "p50_ms": round(sorted_latency[len(sorted_latency) // 2], 2),
            "max_ms": round(max(latencies_ms), 2),
            "candidate_chunks": candidate_rows,
            "unique_top_results": unique_rows,
            "duplicate_chunk_reduction_pct": round(duplicate_reduction * 100, 2),
            "average_capsule_bytes": (
                round(capsule_bytes / capsule_count, 1) if capsule_count else 0.0
            ),
        },
    }


def real_index(args: argparse.Namespace) -> int:
    from semantic_backends import Retriever, chunk_markdown

    retriever = _build_retriever(with_reranker=False)
    # cmd_index never goes through the hybrid wrapper; narrow for index_* calls.
    assert isinstance(retriever, Retriever), "indexing requires base Retriever"

    schema_migration = not retriever.store.has_corpus_metadata()
    indexed_manifest = retriever.store.get_indexed_manifest()
    policy_migration = bool(indexed_manifest) and not _manifest_uses_current_policy(
        indexed_manifest
    )
    empty_index = not indexed_manifest
    rebuild = bool(args.rebuild or schema_migration or policy_migration or empty_index)
    if schema_migration and not args.rebuild:
        warn("index schema lacks corpus metadata; forcing one derived-cache rebuild")
    elif policy_migration and not args.rebuild:
        warn("corpus policy version changed; forcing one derived-cache rebuild")
    elif empty_index and not args.rebuild:
        warn("index is empty; forcing a full derived-cache rebuild")
    if rebuild:
        warn("--rebuild: clearing existing index...")
        retriever.store.clear()

    vault = vault_root()
    warn(f"classifying corpus under {vault}/ with semantic_corpus policy...")
    decisions, files, locators = _corpus_snapshot(vault)
    current_manifest = _record_manifest(decisions, locators)
    locator_tuples = [
        (
            record.path,
            record.text,
            record.mtime,
            record.manifest_fingerprint,
            record.record_id,
        )
        for record in locators
    ]
    warn(
        f"found {len(files)} physical text files and "
        f"{len(locators)} generated raw locator records"
    )

    t0 = time.time()
    update: dict[str, Any]
    if rebuild:
        physical_chunks = retriever.index_files(
            files,
            vault,
            append_only=True,
        )
        locator_chunks = retriever.index_text_records(
            locator_tuples,
            append_only=True,
        )
        elapsed = time.time() - t0
        update = {
            "mode": "rebuild",
            "added_chunks": physical_chunks + locator_chunks,
            "changed_records": len(current_manifest),
            "unchanged_records": 0,
            "removed_records": 0,
            "elapsed_s": round(elapsed, 2),
        }
        warn(
            f"full rebuild: {physical_chunks + locator_chunks} chunks in {elapsed:.1f}s"
        )
    else:
        current_paths = set(current_manifest)
        indexed_paths = set(indexed_manifest)
        changed_paths = {
            path
            for path in current_paths
            if path not in indexed_manifest
            or _manifest_changed(current_manifest[path], indexed_manifest[path])
        }
        removed_paths = sorted(indexed_paths - current_paths)
        prior_changed = sorted(changed_paths & indexed_paths)
        if removed_paths:
            retriever.store.delete_by_path(removed_paths)
        if prior_changed:
            retriever.store.delete_by_path(prior_changed)

        changed_files = [
            item.absolute_path
            for item in decisions
            if item.included
            and item.relative_path in changed_paths
            and item.absolute_path is not None
        ]
        changed_locators = [
            record for record in locators if record.path in changed_paths
        ]
        physical_chunks = retriever.index_files(changed_files, vault)
        locator_chunks = retriever.index_text_records(
            [
                (
                    record.path,
                    record.text,
                    record.mtime,
                    record.manifest_fingerprint,
                    record.record_id,
                )
                for record in changed_locators
            ]
        )
        added = physical_chunks + locator_chunks
        skipped = len(current_manifest) - len(changed_paths)
        removed = len(removed_paths)
        elapsed = time.time() - t0
        update = {
            "mode": "incremental",
            "added_chunks": added,
            "changed_records": len(changed_paths),
            "unchanged_records": skipped,
            "removed_records": removed,
            "elapsed_s": round(elapsed, 2),
        }
        warn(
            f"incremental: {added} chunks added, {skipped} records unchanged, "
            f"{removed} records removed in {elapsed:.1f}s"
        )

    stats = retriever.stats()
    warn(
        f"index stats: {stats.total_documents} chunks, {stats.embedding_dimension}d, model={stats.model_name}"
    )
    corpus_audit = audit_corpus(vault, chunk_estimator=chunk_markdown)
    report = _search_efficiency_report(
        retriever,
        audit=corpus_audit,
        update=update,
    )
    print(
        json.dumps(
            {"search_efficiency": report},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


def cmd_query(args: argparse.Namespace) -> int:
    if args.context and args.format != "json":
        warn("--context requires --format json")
        return 2
    if in_real_mode() and LANCE_DIR == _LANCE_OLD:
        warn(
            "querying legacy index at ~/.cache/reflectl/lance/; run "
            "`uv run scripts/semantic.py index` once to migrate to "
            "~/.cache/atelier/lance/, then `rm -rf ~/.cache/reflectl/lance` to clean up."
        )
    if in_real_mode():
        return real_query(args)
    return stub_query(args)


def cmd_status(args: argparse.Namespace) -> int:
    result = inspect_index_freshness()
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_freshness_summary(result))
    return 2 if result.get("error") else 0


def cmd_corpus(args: argparse.Namespace) -> int:
    """Report corpus boundaries without loading an embedding model."""
    from semantic_backends import chunk_markdown

    result = audit_corpus(vault_root(), chunk_estimator=chunk_markdown)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    summary = result["summary"]
    print(
        "corpus: "
        f"included={summary['included_files']} files, "
        f"excluded={summary['excluded_files']} files, "
        f"records={summary['records']}"
    )
    for scope in VALID_SCOPES:
        row = result["by_scope"][scope]
        print(
            f"{scope}: files={row['files']}, records={row['records']}, "
            f"estimated_chunks={row.get('estimated_chunks', 0)}"
        )
    raw = result["raw"]
    print(
        "raw: "
        f"assets={raw['assets']}, clusters={raw['clusters']}, "
        f"locators={raw['locator_records']}, readable={raw['readable_files']}"
    )
    duplicates = result["exact_duplicates"]
    print(
        "duplicates: "
        f"groups={duplicates['groups']}, files={duplicates['files']}, "
        f"redundant_bytes={duplicates['redundant_bytes']}"
    )
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    # Index always writes to the new path. If a legacy ~/.cache/reflectl/lance/
    # exists, we deliberately do NOT rebuild into it — that would silently keep
    # the user on the old location forever. Force migration here.
    global LANCE_DIR
    LANCE_DIR = _resolve_lance_dir(prefer_new=True)
    with _exclusive_index_lock() as acquired:
        if not acquired:
            warn("another semantic index writer is active; skipping this refresh")
            return 0
        if _LANCE_OLD.exists() and not _LANCE_NEW.exists():
            warn(
                "legacy index exists at ~/.cache/reflectl/lance/; rebuilding at "
                "~/.cache/atelier/lance/ to migrate. The old path can be deleted "
                "after this run completes."
            )
        elif not LANCE_DIR.exists():
            warn(
                "no existing index found; creating ~/.cache/atelier/lance/ "
                "and building index..."
            )
        LANCE_DIR.mkdir(parents=True, exist_ok=True)
        if args.if_stale:
            freshness = inspect_index_freshness(index_dir=LANCE_DIR)
            warn(f"freshness check: {_freshness_summary(freshness)}")
            if freshness["fresh"]:
                warn("index refresh skipped: no corpus drift")
                return 0
        return real_index(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="semantic.py",
        description=(
            f"Local semantic search for zk/ (current mode: {mode_label()}). "
            "STUB mode uses lexical fallback; results are ranked by token "
            "match count and are NOT semantic. REAL mode activates when "
            "~/.cache/atelier/lance/ exists, with a legacy fallback to "
            "~/.cache/reflectl/lance/ for pre-rename installs."
        ),
        epilog="See sources/semantic.md for the full contract.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser("query", help="Run a semantic query.")
    q.add_argument("query", help="Query text (quoted).")
    q.add_argument(
        "--path",
        action="append",
        default=None,
        help="Restrict to a subdirectory, relative to the vault root ($OV); "
        "absolute paths under the vault are accepted and relativized. "
        "Repeatable. Default: the whole vault.",
    )
    q.add_argument(
        "--after",
        default=None,
        help="Only files with mtime >= YYYY-MM-DD.",
    )
    q.add_argument(
        "--before",
        default=None,
        help="Only files with mtime <= YYYY-MM-DD.",
    )
    q.add_argument(
        "--top",
        type=int,
        default=10,
        help="Max results. Default: 10.",
    )
    q.add_argument(
        "--lang",
        choices=["zh", "en", "auto"],
        default="auto",
        help="Query language hint. No-op in stub mode.",
    )
    q.add_argument(
        "--format",
        choices=["tsv", "json"],
        default="tsv",
        help="Output format. Default: tsv.",
    )
    q.add_argument(
        "--scope",
        choices=VALID_SCOPES,
        default=ACTIVE_SCOPE,
        help="Corpus scope. Default: active.",
    )
    q.add_argument(
        "--context",
        action="store_true",
        help="Emit bounded section capsules in JSON output.",
    )
    q.add_argument(
        "--sources",
        default="local",
        help="Comma-separated search sources. Options: local, readwise. "
        "Default: local.",
    )
    q.add_argument(
        "--hybrid",
        action="store_true",
        help="Enable BM25+dense hybrid retrieval (RRF fusion). Slightly slower; "
        "consistently improves recall on keyword-heavy queries.",
    )
    q.add_argument(
        "--rerank",
        choices=["off", "auto", "ce"],
        default="auto",
        help="Reranker mode. 'ce' = BGE-reranker-v2-m3 cross-encoder (best "
        "quality, ~500ms extra per query on MPS). 'auto' = ce when "
        "SEMANTIC_RERANK_CE=1 else off. 'off' = tier/recency only.",
    )
    q.set_defaults(func=cmd_query)

    s = sub.add_parser(
        "status",
        help="Inspect index freshness without loading the embedding model.",
    )
    s.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Default: text.",
    )
    s.set_defaults(func=cmd_status)

    c = sub.add_parser(
        "corpus",
        help="Audit corpus scopes, exclusions, raw locators, and duplicates without a model.",
    )
    c.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Default: text.",
    )
    c.set_defaults(func=cmd_corpus)

    i = sub.add_parser("index", help="Build or refresh the embedding index.")
    index_mode = i.add_mutually_exclusive_group()
    index_mode.add_argument(
        "--rebuild",
        action="store_true",
        help="Force full rebuild.",
    )
    index_mode.add_argument(
        "--if-stale",
        action="store_true",
        help="Run incremental indexing only when a lightweight drift check finds changes.",
    )
    i.set_defaults(func=cmd_index)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
