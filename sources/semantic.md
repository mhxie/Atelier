# Local QMD Search

`scripts/semantic.py` is the Atelier boundary around the pinned QMD SDK.
QMD owns scanning, Markdown chunking, incremental updates and deletions,
embeddings, keyword/vector fusion, and model reranking. The Python adapter
owns source scopes, local-only model readiness, bounded output, and callers.
The canonical vault is never modified by indexing or querying.

## Setup and hardware

Install Node >=22 in a system or Homebrew prefix, run `npm ci`, and set `OV` to
the canonical vault. The adapter does not resolve Node from the caller's `PATH`.
Copy `semantic.toml.example` to the gitignored `semantic.toml` if overrides
are needed. Presets live in `harness/retrieval.toml`.

| Profile | Embedding model | Parallel contexts | Batch documents | Embed / rerank context |
|---|---|---:|---:|---:|
| `m3-16gb` (default) | Qwen3-Embedding 0.6B Q8 | 1 | 4 | 2048 / 4096 |
| `m5-64gb` (future, not hardware-validated) | Qwen3-Embedding 4B Q8 | 2 | 16 | 4096 / 8192 |

The larger preset is a configurable starting point, not a memory-usage guarantee
or measured quality/speed improvement. Both retain the 0.6B reranker.
QMD may further limit resources. Prefect still runs one local job at a time.
Changing embedding model or embedding context selects a separate index, so
different vector dimensions never share a database. Old derived indexes are
retained for rollback; no cache cleanup is implicit.

```bash
python3 scripts/semantic.py init --download-models
python3 scripts/semantic.py index
python3 scripts/semantic.py status --format json
python3 scripts/semantic.py query "how should I handle rate limits?"
```

Only `init --download-models` downloads GGUF models. Queries and normal index
updates require existing local model files. The model pool and per-vault
indexes live under `~/.cache/atelier/qmd/` (respecting `XDG_CACHE_HOME`);
`ATELIER_QMD_HOME` can select another machine-local cache outside the vault
and repository. For the future machine, set `profile = "m5-64gb"` in
`semantic.toml`, then repeat initialization and indexing.
`ATELIER_QMD_PROFILE` is a one-process profile override.

The local Prefect `semantic-index` deployment runs at 07:30 and 19:30 in the
machine's IANA timezone, with a bounded timeout and one retry for this
retry-safe derived-cache job. Prefect serializes its deployments and records
state and logs. Provision the pinned Node dependencies and selected model
cache before enabling it. These source declarations do not activate a service.

Inference uses only installed native bindings; it never downloads or compiles
a backend. Metal needs host GPU access: a restricted sandbox can reject its
command queue even when normal host execution succeeds. Report that failure
or explicitly use lexical mode; do not silently substitute empty results.

## Commands

| Command | Meaning |
|---|---|
| `init [--download-models]` | Write the derived QMD collection config; model download is opt-in. |
| `index` | QMD incrementally reconciles source files and embeddings. Skipped files or embedding errors fail the command. |
| `index --lexical-only` | Update text without loading a model. This does not claim vector readiness. |
| `status [--format json\|text]` | Native QMD counts, pending embeddings, local model presence and `ready`; no corpus freshness scan. |
| `query TEXT` | Hybrid keyword + vector retrieval and reranking, with bounded JSON results. |
| `query TEXT --mode lexical` | Explicit model-free keyword search; never an automatic fallback. |
| `query TEXT --mode vector` | Embedding retrieval without keyword fusion or reranking. |

Query options: `--top 1..100`, `--scope`, repeatable `--path`, `--after`,
`--before`, `--format json|tsv`, `--no-rerank`, and `--expand`.
By default the same text is supplied as typed lexical and vector queries,
without automatic rewriting. `--expand` explicitly enables QMD's local
query-expansion model. Test Chinese, English and mixed-language framing
against relevant source documents; a synthetic fixture is not a vault benchmark.

Path and file-mtime date filters apply to at most 200 retrieved candidates.
They never broaden source access, but can return fewer results than requested
or miss a match outside that candidate set. Use scoped `rg` for exhaustive
path/date inspection or to establish that an exact document is absent.

## Source scopes

`active` is the default collection of current authored Markdown.
`raw` contains readable Markdown/text/CSV/HTML under a raw directory;
`archive`, `inbox`, and `process` select parked notes, pending captures,
and session traces. `all` is the union, not a privacy override.
Root archive/process/meta paths respect the path registry.

All collections exclude operational directories (cache, metadata, routine
prompts, private tools, hidden directories and dependency trees) and orphan
stubs. QMD does not follow symlinks; the adapter also rejects replaced
symlinks, out-of-vault paths, deleted sources, and scope-mismatched results.
If `harness/paths.local.toml` sets `raw_store`, the `raw` scope and `secure/`
notes (searched as `active`) are indexed from that mirror of vault paths; a hit
may then cross one `raw` or `secure` folder link onto the same path there.
Read the original source before quoting. Scope is provenance, not certification.

Readwise uses its own explicit connector/CLI. Binary raw locator generation,
custom trust/recency score adjustment, stub fallback, corpus auditing,
old backend options, and legacy context capsules have been retired.

## Results and failures

JSON is always a list of bounded result objects: `path`, `scope`, `title`,
`line`, `snippet` (up to 600 characters), `score`, `source: local`,
`backend: qmd`, `score_kind`, and `representation`. `score_kind` names the
pipeline that produced the score, so `--mode hybrid --no-rerank` reports
`hybrid-no-rerank` rather than `hybrid`.
Paths are vault-relative; one row per source file.
TSV contains path, score and scope. Output goes to stdout; diagnostics to stderr.

A score ranks this retrieval mode's candidates. It is not confidence, a
probability, or interchangeable with old similarity thresholds.
Redundancy retrieval produces candidates for content review; QMD findings
route to human review, never the legacy score-based automatic merge band.

Exit 0 means the requested operation succeeded, including an honest empty
search. Exit 2 means invalid arguments, missing/incompatible state or models,
child failure, invalid output, or incomplete indexing. A missing index does
not get created by `query` or `status`. Status reports `freshness: unchecked`:
run `index` on schedule to reconcile sources, not a homemade manifest scan.
`ready` checks stored vectors and model files, not a live inference probe.

## Verification and rollout

`tests/test_qmd.py` covers adapter failure and source boundaries, including
real QMD keyword indexing/update/delete when npm dependencies are installed.
The independent paper-cache extraction checks remain in `tests/test_paper_cache.py`.
The pinned QMD 2.8.3 bridge shares the SDK and tokenizer model instance (matching
QMD's CLI), forwards the index deadline to its embed session, and rejects
null/partial embeddings and unavailable requested reranking.
Recheck these adapter guards when upgrading the dependency.

```bash
OV="$PWD/tests/fixtures/qmd" python3 scripts/semantic.py index
OV="$PWD/tests/fixtures/qmd" python3 scripts/semantic_eval.py run \
  --gold tests/fixtures/qmd/quality.json
```

The evaluator consumes explicit query/target pairs and reports retrieval
metrics. It no longer generates gold questions from vault links.
`scripts/_evalset.json` remains an optional gitignored per-vault input.

A source patch does not activate a scheduler or migrate a live vault.
Review it, provision the intended machine's model cache, build the new index,
and validate representative queries before switching active callers.
The prior Lance caches are left untouched.
