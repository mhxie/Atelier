"""smoke_semantic.py: paper cache and semantic index smoke checks.

Split out of harness_smoke.py; harness_smoke.py re-exports every name so callers and tests are unchanged.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import subprocess
import tempfile
from pathlib import Path
import semantic
import semantic_backends
import semantic_corpus
import semantic_eval

from smoke_common import (  # noqa: E402
    PYTHON,
    ROOT,
    SmokeFailure,
    expect,
)


def check_paper_cache() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    source_doc = (ROOT / "sources" / "local-papers.md").read_text(encoding="utf-8")
    read_command = (ROOT / ".claude" / "commands" / "read.md").read_text(
        encoding="utf-8"
    )
    expect("/tmp/" in gitignore.splitlines(), "repo tmp containment rule is missing")
    expect(
        "Never write repo-relative `tmp/`" in claude,
        "shared scratch boundary is missing from CLAUDE.md",
    )
    for document, label in (
        (source_doc, "local paper source doc"),
        (read_command, "read command"),
    ):
        expect(
            "scripts/paper_cache.py" in document,
            f"{label} does not route PDF extraction through paper_cache.py",
        )

    with tempfile.TemporaryDirectory(prefix="atelier-paper-cache-") as temp_dir:
        temp = Path(temp_dir)
        vault = temp / "vault"
        papers = vault / "papers"
        papers.mkdir(parents=True)
        (vault / "preprints").mkdir()
        source = papers / "Example Paper.pdf"
        source.write_text("fixture pdf", encoding="utf-8")

        fake_bin = temp / "bin"
        fake_bin.mkdir()
        fake_pdftotext = fake_bin / "pdftotext"
        fake_pdftotext.write_text(
            f"#!{PYTHON}\n"
            "from pathlib import Path\n"
            "import sys\n"
            "source = Path(sys.argv[-2]).read_text(encoding='utf-8')\n"
            "content = '   \\n' if source == 'empty fixture' else 'EXTRACTED\\n' + source\n"
            "Path(sys.argv[-1]).write_text(content, encoding='utf-8')\n",
            encoding="utf-8",
        )
        fake_pdftotext.chmod(0o755)
        env = os.environ | {
            "OV": str(vault),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        }

        first = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(source), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(first.returncode == 0, f"paper cache extraction failed: {first.stderr}")
        first_payload = json.loads(first.stdout)
        expect(
            first_payload["status"] == "extracted",
            "paper cache did not extract on miss",
        )
        cache_dir = vault / "cache" / "example-paper"
        expect(
            (cache_dir / "paper.txt").read_text(encoding="utf-8")
            == "EXTRACTED\nfixture pdf",
            "paper cache text output drift",
        )
        expect((cache_dir / "index.md").is_file(), "paper cache index is missing")
        expect(
            (cache_dir / "source.json").is_file(),
            "paper cache source signature is missing",
        )

        second = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(source), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(second.returncode == 0, f"paper cache reuse failed: {second.stderr}")
        expect(
            json.loads(second.stdout)["status"] == "cached",
            "fresh paper cache was rebuilt",
        )

        colliding = vault / "preprints" / "Example Paper.pdf"
        colliding.write_text("different fixture pdf", encoding="utf-8")
        collision = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(colliding), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(collision.returncode == 2, "paper cache overwrote a same-slug PDF cache")
        expect(
            (cache_dir / "paper.txt").read_text(encoding="utf-8")
            == "EXTRACTED\nfixture pdf",
            "paper cache collision changed the original extraction",
        )

        empty_source = papers / "Empty.pdf"
        empty_source.write_text("empty fixture", encoding="utf-8")
        empty = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(empty_source), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(empty.returncode == 2, "paper cache accepted an empty text extraction")
        expect(
            not (vault / "cache" / "empty" / "paper.txt").exists(),
            "paper cache retained an empty extraction",
        )

        outside = temp / "outside.pdf"
        outside.write_text("not canonical", encoding="utf-8")
        rejected = subprocess.run(
            [PYTHON, "scripts/paper_cache.py", str(outside)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        expect(
            rejected.returncode == 2, "paper cache accepted a PDF outside the L3 store"
        )

def check_semantic_cache_first() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-hf-cache-") as temp_dir:
        hub = Path(temp_dir)
        repository = hub / "models--Example--embedding"
        revision = "abc123"
        snapshot = repository / "snapshots" / revision
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}\n", encoding="utf-8")
        (snapshot / "modules.json").write_text("[]\n", encoding="utf-8")
        refs = repository / "refs"
        refs.mkdir()
        (refs / "main").write_text(revision, encoding="utf-8")
        original_cache = os.environ.get("HF_HUB_CACHE")
        os.environ["HF_HUB_CACHE"] = str(hub)
        try:
            resolved = semantic_backends._local_model_snapshot("Example/embedding")
            source, local_only = semantic_backends._sentence_transformer_source(
                "Example/embedding"
            )
            expect(
                resolved == str(snapshot.resolve())
                and source == str(snapshot.resolve())
                and local_only is True,
                "semantic embedder did not prefer a complete local snapshot",
            )
            missing_source, missing_local_only = (
                semantic_backends._sentence_transformer_source("Example/not-cached")
            )
            expect(
                missing_source == "Example/not-cached" and missing_local_only is False,
                "semantic embedder disabled first-time download without offline mode",
            )
        finally:
            if original_cache is None:
                os.environ.pop("HF_HUB_CACHE", None)
            else:
                os.environ["HF_HUB_CACHE"] = original_cache

def check_semantic_maintenance() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-semantic-freshness-") as temp_dir:
        vault = Path(temp_dir)
        stable = vault / "stable.md"
        modified = vault / "modified.md"
        added = vault / "added.md"
        empty = vault / "empty.md"
        whitespace = vault / "whitespace.md"
        stable.write_text("stable\n", encoding="utf-8")
        modified.write_text("modified\n", encoding="utf-8")
        added.write_text("added\n", encoding="utf-8")
        empty.write_text("", encoding="utf-8")
        whitespace.write_text(" \n\t", encoding="utf-8")

        baseline = 1_700_000_000.0
        os.utime(stable, (baseline, baseline))
        os.utime(modified, (baseline + 10, baseline + 10))
        os.utime(added, (baseline + 20, baseline + 20))
        current_manifest, physical_count, _, unreadable = (
            semantic._current_corpus_manifest(vault)
        )
        indexed = {
            "stable.md": {
                "mtime": baseline,
                "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                    "active",
                    "authored",
                    baseline,
                ),
            },
            "modified.md": {
                "mtime": baseline,
                "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                    "active",
                    "authored",
                    baseline,
                ),
            },
            "removed.md": {
                "mtime": baseline,
                "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                    "active",
                    "authored",
                    baseline,
                ),
            },
        }
        stale = semantic._freshness_from_manifest(
            current_manifest,
            indexed,
            physical_count=physical_count,
            unreadable=unreadable,
        )
        expect(
            stale["fresh"] is False
            and stale["current_files"] == 3
            and stale["new"] == 1
            and stale["modified"] == 1
            and stale["removed"] == 1
            and stale["unreadable"] == 0,
            f"semantic freshness drift classification failed: {stale}",
        )

        fresh = semantic._freshness_from_manifest(
            current_manifest,
            current_manifest,
            physical_count=physical_count,
            unreadable=unreadable,
        )
        expect(
            fresh["fresh"] is True,
            f"semantic freshness rejected a current index: {fresh}",
        )
        immediate_edit = semantic._freshness_from_manifest(
            {
                "active.md": {
                    "mtime": baseline + 0.5,
                    "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                        "active",
                        "authored",
                        baseline + 0.5,
                    ),
                }
            },
            {
                "active.md": {
                    "mtime": baseline,
                    "manifest_fingerprint": semantic.physical_manifest_fingerprint(
                        "active",
                        "authored",
                        baseline,
                    ),
                }
            },
            physical_count=1,
        )
        expect(
            immediate_edit["modified"] == 1 and immediate_edit["fresh"] is False,
            "sub-second physical-file edits did not invalidate semantic freshness",
        )

    args = semantic.build_parser().parse_args(["index", "--if-stale"])
    expect(
        args.if_stale is True and args.rebuild is False,
        "semantic index parser lost the drift-gated maintenance mode",
    )

    runner = (ROOT / "scripts" / "semantic_index_runner.sh").read_text(encoding="utf-8")
    for fragment in (
        'routine_owner.py" check --json',
        "HF_HUB_OFFLINE=1",
        "TRANSFORMERS_OFFLINE=1",
        "command_timeout.py",
        "/usr/bin/caffeinate",
        "uv run --offline --frozen scripts/semantic.py index --if-stale",
    ):
        expect(
            fragment in runner,
            f"semantic maintenance runner missing contract fragment: {fragment}",
        )
    expect(
        "codex" not in runner.lower(), "deterministic index maintenance invokes Codex"
    )

    plist_path = ROOT / "scripts" / "launchd" / "com.atelier.semantic-index.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    expect(
        plist.get("Label") == "com.atelier.semantic-index"
        and plist.get("RunAtLoad") is True
        and plist.get("StartCalendarInterval")
        == [
            {"Hour": 7, "Minute": 30},
            {"Hour": 19, "Minute": 30},
        ],
        "semantic maintenance launchd schedule drift",
    )
    arguments = plist.get("ProgramArguments", [])
    expect(
        any("semantic_index_runner.sh" in str(value) for value in arguments),
        "semantic maintenance plist does not invoke its deterministic runner",
    )

    for path in (
        ROOT / ".claude" / "commands" / "autoevo-nightly.md",
        ROOT / "protocols" / "autoevo.md",
    ):
        expect(
            "lazy rebuild" not in path.read_text(encoding="utf-8").lower(),
            f"{path.relative_to(ROOT)} still claims query refreshes the index",
        )

def check_semantic_corpus_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="atelier-semantic-corpus-") as temp_dir:
        vault = Path(temp_dir)
        fixture_files = {
            "active.md": "## Active\ncurrent authored knowledge\n",
            "duplicate.md": "## Active\ncurrent authored knowledge\n",
            "archive/old.md": "## Old\ncold history\n",
            "inbox/pending.md": "## Pending\nunreviewed capture\n",
            "health/nutrition/inbox/pending.md": "## Pending\nnested capture\n",
            "sessions/2099-01-01-test.md": "## Continuity\nprocess only\n",
            "cache/noise.md": "cache noise\n",
            "wip/cache/noise.md": "nested cache noise\n",
            "_meta/noise.md": "operational noise\n",
            "_routine_prompts/noise.md": "prompt noise\n",
            "career/_tools/runs/noise.md": "generated run log\n",
            ".trash/noise.md": "trash noise\n",
            "archive/orphan-stubs/noise.md": "stub noise\n",
            "research/tool/node_modules/pkg/README.md": "dependency docs\n",
            "finance/raw/tax-2099/receipt.txt": "readable raw receipt\n",
        }
        for relative, content in fixture_files.items():
            path = vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        binary = b"same-binary-provenance"
        for relative in (
            "finance/raw/tax-2099/receipt.pdf",
            "travel/raw/confirmations/receipt-copy.pdf",
        ):
            path = vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(binary)
        (vault / "empty.md").write_text("", encoding="utf-8")
        (vault / "whitespace.md").write_text(" \n\t", encoding="utf-8")

        decisions = tuple(semantic_corpus.iter_file_decisions(vault))
        by_path = {item.relative_path: item for item in decisions}
        expect(
            by_path["active.md"].included
            and by_path["archive/old.md"].scope == "archive"
            and by_path["inbox/pending.md"].scope == "inbox"
            and by_path["health/nutrition/inbox/pending.md"].scope == "inbox"
            and by_path["sessions/2099-01-01-test.md"].scope == "process"
            and by_path["finance/raw/tax-2099/receipt.txt"].scope == "raw",
            "central corpus scope classification drift",
        )
        for excluded_path in (
            "cache/noise.md",
            "wip/cache/noise.md",
            "_meta/noise.md",
            "_routine_prompts/noise.md",
            "career/_tools/runs/noise.md",
            ".trash/noise.md",
            "archive/orphan-stubs/noise.md",
            "research/tool/node_modules/pkg/README.md",
            "empty.md",
            "whitespace.md",
        ):
            expect(
                not by_path[excluded_path].included,
                f"hard or empty corpus path was included: {excluded_path}",
            )

        active = tuple(
            semantic_corpus.iter_corpus_records(
                vault,
                scope="active",
                files=decisions,
            )
        )
        raw = tuple(
            semantic_corpus.iter_corpus_records(
                vault,
                scope="raw",
                files=decisions,
            )
        )
        expect(
            any(row.representation == "raw_locator" for row in active)
            and not any(row.representation == "raw_text" for row in active),
            "active scope did not substitute raw locators for full raw text",
        )
        expect(
            any(row.representation == "raw_locator" for row in raw)
            and any(row.representation == "raw_text" for row in raw),
            "raw scope omitted locator or readable raw content",
        )
        locator = next(row for row in active if row.representation == "raw_locator")
        expect(
            "same-binary-provenance" not in locator.text
            and semantic_corpus.scope_matches(locator, "active")
            and semantic_corpus.scope_matches(locator, "raw"),
            "raw locator extracted binary content or lost dual-scope visibility",
        )

        audit = semantic_corpus.audit_corpus(
            vault,
            chunk_estimator=semantic_backends.chunk_markdown,
        )
        expect(
            audit["by_exclusion_reason"]["dependency_tree"]["files"] == 1,
            "dependency-tree exclusion missing from corpus audit",
        )
        expect(
            audit["by_exclusion_reason"]["operational_tools"]["files"] == 1,
            "nested operational-tool exclusion missing from corpus audit",
        )
        expect(
            audit["exact_duplicates"]["basis"] == "all_regular_physical_files"
            and audit["exact_duplicates"]["groups"] >= 2
            and audit["exact_duplicates"]["included_text"]["groups"] >= 1,
            "all-file and included-text duplicate summaries did not reconcile",
        )

        fingerprint_drift = semantic._freshness_from_manifest(
            {
                locator.path: {
                    "mtime": locator.mtime,
                    "manifest_fingerprint": "new",
                }
            },
            {
                locator.path: {
                    "mtime": locator.mtime,
                    "manifest_fingerprint": "old",
                }
            },
            physical_count=0,
        )
        expect(
            fingerprint_drift["modified"] == 1 and fingerprint_drift["fresh"] is False,
            "raw locator fingerprint drift did not invalidate freshness",
        )
        older_mtime_drift = semantic._freshness_from_manifest(
            {
                "active.md": {
                    "mtime": 100.0,
                    "manifest_fingerprint": "",
                }
            },
            {
                "active.md": {
                    "mtime": 200.0,
                    "manifest_fingerprint": "",
                }
            },
            physical_count=1,
        )
        expect(
            older_mtime_drift["modified"] == 1 and older_mtime_drift["fresh"] is False,
            "restored older mtimes did not invalidate freshness",
        )

    query_args = semantic.build_parser().parse_args(["query", "fixture"])
    hybrid_query_args = semantic.build_parser().parse_args(
        ["query", "fixture", "--hybrid"]
    )
    expect(
        query_args.scope == "active"
        and query_args.sources == "local"
        and query_args.context is False,
        "semantic query defaults are not local active bounded opt-in",
    )
    expect(
        hybrid_query_args.hybrid
        and semantic._locator_backfill_enabled(hybrid_query_args.scope)
        and not semantic._locator_backfill_enabled("archive"),
        "hybrid retrieval disabled raw-locator navigation backfill",
    )
    eval_args = semantic_eval.build_parser().parse_args(["run"])
    expect(
        eval_args.scope == "active" and semantic_eval.EVAL_SCOPES == ("active", "all"),
        "semantic evaluation escaped its active/all gold-set contract",
    )
    hits = [
        semantic.QueryHit(
            path="same.md",
            score=0.9,
            chunk_id=0,
            chunk_text="x" * 700,
        ),
        semantic.QueryHit(
            path="same.md",
            score=0.8,
            chunk_id=1,
            chunk_text="duplicate chunk",
        ),
        *[
            semantic.QueryHit(
                path=f"@raw-locator/domain/raw/cluster-{index}",
                score=0.7 - index * 0.01,
                chunk_text="Raw cluster locator",
                tier="L1",
                representation="raw_locator",
            )
            for index in range(3)
        ],
        semantic.QueryHit(path="other.md", score=0.5, chunk_text="other"),
    ]
    collapsed = semantic._collapse_hits(hits, top=10, requested_scope="active")
    expect(
        len([hit for hit in collapsed if hit.path == "same.md"]) == 1
        and len([hit for hit in collapsed if hit.representation == "raw_locator"]) == 2,
        "result collapse or active raw-locator cap drift",
    )
    expect(
        semantic._path_matches(
            "@raw-locator/finance/raw/tax-2099",
            ["finance"],
        )
        and semantic_backends._passes_filters(
            semantic_backends.SearchResult(
                path="@raw-locator/finance/raw/tax-2099",
                score=0.9,
                representation="raw_locator",
            ),
            {"path_prefix": ["finance/raw"]},
        ),
        "raw locator did not honor its virtual source path prefix",
    )
    locator_sql = semantic_backends._path_prefix_where_clause(
        ["finance/raw"],
        lambda value: value,
    )
    expect(
        "@raw-locator/finance/raw/%" in locator_sql,
        "Lance path SQL omitted the raw-locator virtual alias",
    )

    delete_filters: list[str] = []

    class FakeDeleteTable:
        @staticmethod
        def delete(predicate: str) -> None:
            delete_filters.append(predicate)

    delete_store = semantic_backends.LanceStore.__new__(semantic_backends.LanceStore)
    delete_store._table = FakeDeleteTable()
    delete_paths = [f"notes/item-{index}.md" for index in range(300)]
    expect(
        delete_store.delete_by_path(delete_paths) == len(delete_paths)
        and len(delete_filters) == 3
        and all(
            predicate.count('path = "')
            <= semantic_backends.LanceStore.DELETE_PATH_BATCH_SIZE
            for predicate in delete_filters
        ),
        "Lance path deletion did not bound policy-wide predicates",
    )

    failed_delete_filters: list[str] = []

    class FakePartialDeleteTable:
        @staticmethod
        def delete(predicate: str) -> None:
            failed_delete_filters.append(predicate)
            if len(failed_delete_filters) == 2:
                raise RuntimeError("fixture delete failure")

    failed_delete_store = semantic_backends.LanceStore.__new__(
        semantic_backends.LanceStore
    )
    failed_delete_store._table = FakePartialDeleteTable()
    try:
        failed_delete_store.delete_by_path(delete_paths)
    except RuntimeError:
        pass
    else:
        raise SmokeFailure("partial Lance path deletion did not abort")
    expect(
        len(failed_delete_filters) == 2,
        "Lance path deletion continued after a partial batch failure",
    )

    legacy_active_sql = semantic_corpus.scope_sql(
        "active",
        has_metadata_columns=False,
    )
    expect(
        "_tools" in legacy_active_sql
        and "%/inbox/%" in legacy_active_sql
        and "%/cache/%" in legacy_active_sql,
        "legacy scope SQL drifted from nested operational and inbox policy",
    )

    bm25_trace: dict[str, object] = {}

    class FakeLanceQuery:
        def where(self, predicate: str) -> "FakeLanceQuery":
            bm25_trace["where"] = predicate
            return self

        def select(self, columns: list[str]) -> "FakeLanceQuery":
            bm25_trace["columns"] = columns
            return self

        def limit(self, count: int) -> "FakeLanceQuery":
            bm25_trace["limit"] = count
            return self

        @staticmethod
        def to_pandas() -> str:
            return "scope-frame"

    class FakeLanceTable:
        @staticmethod
        def count_rows() -> int:
            return 99

        @staticmethod
        def search() -> FakeLanceQuery:
            return FakeLanceQuery()

    class FakeLanceStore:
        _table = FakeLanceTable()

        @staticmethod
        def has_corpus_metadata() -> bool:
            return True

    expect(
        semantic_backends._bm25_scope_frame(FakeLanceStore(), "active") == "scope-frame"
        and bm25_trace["where"] == "scope = 'active'"
        and bm25_trace["limit"] == 99,
        "BM25 materialized Lance before applying the selected scope",
    )
    locator_candidates = [
        semantic_backends.SearchResult(
            path="@raw-locator/finance/raw/tax",
            score=0.0,
            chunk_text="Filename terms: unique-token",
            tier="L1",
            scope="active",
            representation="raw_locator",
        ),
        semantic_backends.SearchResult(
            path="@raw-locator/travel/raw/receipts",
            score=0.0,
            chunk_text="Filename terms: hotel receipt",
            tier="L1",
            scope="active",
            representation="raw_locator",
        ),
    ]
    locator_ranked = semantic_backends._rank_raw_locator_results(
        locator_candidates,
        "unique-token",
        top_k=10,
        filters={"scope": "active"},
    )
    expect(
        [result.path for result in locator_ranked] == ["@raw-locator/finance/raw/tax"],
        "filename-only query did not discover its raw locator",
    )
    generic_locator_ranked = semantic_backends._rank_raw_locator_results(
        [
            semantic_backends.SearchResult(
                path="@raw-locator/archive/education/raw/training",
                score=0.0,
                chunk_text="Filename terms: training",
                tier="L1",
                scope="active",
                representation="raw_locator",
            )
        ],
        "distributed training architecture",
        top_k=10,
        filters={"scope": "active"},
    )
    expect(
        generic_locator_ranked == [],
        "multi-word concept query triggered a locator on one rare token",
    )
    capsule = semantic._capsule(collapsed[0], "active")
    expect(
        len(capsule["snippet"]) <= 600 and capsule["truncated"] is True,
        "result capsule exceeded the 600-character source budget",
    )
    legacy_ranked = semantic._rank_hits(
        hits,
        top=2,
        requested_scope="active",
    )
    active_ranked = semantic._rank_hits(
        hits,
        top=10,
        requested_scope="active",
    )
    expect(
        [hit.path for hit in legacy_ranked] == ["same.md", "same.md"]
        and len([hit for hit in active_ranked if hit.representation == "raw_locator"])
        == 2
        and semantic._rank_hits(
            hits,
            top=0,
            requested_scope="active",
        )
        == []
        and semantic._collapse_hits(hits, top=0, requested_scope="active") == [],
        "legacy chunk ranking or non-positive top handling drift",
    )
    authored_page = [
        semantic.QueryHit(
            path=f"note-{index}.md",
            score=0.9 - index * 0.03,
            chunk_text="authored",
        )
        for index in range(10)
    ]
    exact_locator = semantic.QueryHit(
        path="@raw-locator/finance/raw/tax",
        score=1.0,
        chunk_text="Filename terms: unique-token",
        tier="L1",
        representation="raw_locator",
    )
    backfilled = semantic._backfill_locator_hit(
        authored_page,
        [exact_locator],
        top=10,
        requested_scope="active",
        collapse=True,
    )
    chunk_backfilled = semantic._backfill_locator_hit(
        authored_page,
        [exact_locator],
        top=10,
        requested_scope="active",
        collapse=False,
    )
    authored_first = semantic._rank_hits(
        [
            semantic.QueryHit(
                path="authored.md",
                score=0.1,
                chunk_text="authored",
            ),
            exact_locator,
        ],
        top=2,
        requested_scope="active",
    )
    expect(
        len(backfilled) == 10
        and [hit.path for hit in backfilled[:9]]
        == [hit.path for hit in authored_page[:9]]
        and backfilled[-1].path == exact_locator.path
        and backfilled[-1].score <= backfilled[-2].score
        and chunk_backfilled[-1].path == exact_locator.path
        and chunk_backfilled[-1].score <= chunk_backfilled[-2].score
        and [hit.path for hit in authored_first] == ["authored.md", exact_locator.path]
        and authored_first[0].score >= authored_first[1].score,
        "raw locator competed with authored or reranked results instead of backfilling",
    )
    expect(
        semantic._backfill_locator_hit(
            authored_page,
            [exact_locator],
            top=1,
            requested_scope="active",
            collapse=True,
        )[0].path
        == "note-0.md",
        "top-1 active search displaced its authored result with a locator",
    )
    expect(
        semantic._local_candidate_count(
            10,
            cross_encoder=False,
            collapse=False,
        )
        == 10
        and semantic._local_candidate_count(
            10,
            cross_encoder=False,
            collapse=True,
        )
        == 60
        and semantic._local_candidate_count(
            10,
            cross_encoder=True,
            collapse=False,
        )
        == 30,
        "candidate widening escaped context, federation, or rerank modes",
    )
    current_manifest = semantic._record_manifest(
        decisions,
        tuple(record for record in active if record.representation == "raw_locator"),
    )
    active_decision = next(
        item for item in decisions if item.relative_path == "active.md"
    )
    expect(
        current_manifest["active.md"]["manifest_fingerprint"]
        == semantic.physical_manifest_fingerprint(
            "active",
            "authored",
            active_decision.mtime,
        )
        and semantic._manifest_uses_current_policy(current_manifest),
        "corpus policy version was not persisted in the index manifest",
    )
    scope_drift = semantic._freshness_from_manifest(
        {
            "active.md": {
                "mtime": 100.0,
                "manifest_fingerprint": semantic.corpus_metadata_fingerprint(
                    "inbox",
                    "authored",
                ),
            }
        },
        {
            "active.md": {
                "mtime": 100.0,
                "manifest_fingerprint": semantic.corpus_metadata_fingerprint(
                    "active",
                    "authored",
                ),
            }
        },
        physical_count=1,
    )
    expect(
        scope_drift["modified"] == 1 and scope_drift["fresh"] is False,
        "scope-only corpus drift did not invalidate freshness",
    )
    external_capsule = semantic._capsule(
        semantic.QueryHit(
            path="readwise://fixture",
            score=0.9,
            chunk_text="external",
            source="readwise",
            scope="external",
        ),
        "active",
    )
    expect(
        external_capsule["scope"] == "external",
        "external capsule was mislabeled with the requested local scope",
    )
    legacy_stub_json = io.StringIO()
    with contextlib.redirect_stdout(legacy_stub_json):
        semantic._emit_hits(
            [hits[0]],
            output_format="json",
            context=False,
            requested_scope="active",
            include_source=False,
        )
    expect(
        set(json.loads(legacy_stub_json.getvalue())[0])
        == {"path", "score", "matched_tokens"},
        "non-context stub JSON gained fields outside the legacy contract",
    )

    class FakeStats:
        total_chunks = 12
        model_name = "fixture"

    class FakeStore:
        @staticmethod
        def has_corpus_metadata() -> bool:
            return False

    class FakeRetriever:
        store = FakeStore()

        def query(
            self,
            _query: str,
            *,
            top_k: int,
            filters: dict[str, object],
        ) -> list[semantic_backends.SearchResult]:
            expect(top_k == 30 and filters == {"scope": "active"}, "probe drift")
            return [
                semantic_backends.SearchResult(
                    path="same.md",
                    score=0.9,
                    chunk_id=0,
                    chunk_text="first",
                ),
                semantic_backends.SearchResult(
                    path="same.md",
                    score=0.8,
                    chunk_id=1,
                    chunk_text="second",
                ),
                semantic_backends.SearchResult(
                    path="other.md",
                    score=0.7,
                    chunk_text="other",
                ),
            ]

        @staticmethod
        def stats() -> FakeStats:
            return FakeStats()

    efficiency = semantic._search_efficiency_report(
        FakeRetriever(),
        audit=audit,
        update={"mode": "fixture"},
    )
    expect(
        efficiency["corpus"]["default_scope_reduction_pct"] >= 0
        and efficiency["query_probe"]["queries"] == 3
        and efficiency["query_probe"]["duplicate_chunk_reduction_pct"] > 0,
        "post-index search efficiency report lost scope, latency, or dedup metrics",
    )
