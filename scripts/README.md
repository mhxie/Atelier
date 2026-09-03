# scripts/

Executable tooling for the Atelier knowledge layer. All scripts are stdlib-only (or stdlib + one documented dependency), deterministic, and runnable from the repo root with project-relative paths.

## Inventory

| Script | Purpose | Phase | Deps |
|---|---|---|---|
| `semantic.py` | Scoped local semantic search over `$OV/` with bounded context capsules, model-free freshness inspection, drift-gated incremental indexing, raw locator cards, and an automatic search-efficiency report after every real update; lexical fallback when the index is absent | B.5 | `lancedb`, `sentence-transformers` (optional; falls back to lexical) |
| `semantic_backends.py` | Backend implementations for semantic.py (LanceDB embedding backend, lexical fallback) | B.5 | `lancedb`, `sentence-transformers` (optional) |
| `semantic_corpus.py` | Deterministic semantic corpus policy: scope classification, exclusions, raw locator generation, duplicate accounting, and read-only audits | B.5 | stdlib |
| `semantic_eval.py` | Offline evaluation harness for semantic.py — builds a wikilink-derived gold set from the vault and computes retrieval metrics | B.5 | `lancedb`, `sentence-transformers` |
| `semantic_index_runner.sh` | Owner-gated, offline, timeout-bounded launchd wrapper for `semantic.py index --if-stale`; loads no embedding model when the corpus is current | ops | `uv`, `caffeinate`, `routine_owner.py` |
| `config.py` | Loads device-dependent semantic-index parameters from gitignored `semantic.toml`; safe defaults when the file is missing | B.5 | stdlib |
| `_paths.py` | Shared path-resolution helpers — fail-loud `$OV` resolution plus logical-tier → physical-segment mapping from `harness/paths.toml` (+ gitignored local layer) | ops | stdlib |
| `paper_cache.py` | Extracts an L3 paper PDF into a reusable L1 `<paths.cache>/<slug>/paper.txt` with source freshness metadata; refuses repo-local scratch and non-vault sources | ops | stdlib, `pdftotext` CLI |
| `dining_audit.py` | Validates branch addresses/lifecycles, the canonical 14-column meal-history schema, event-date order, local links, eligibility/live-state separation, health taxonomy, and per-person arithmetic; can render sourced recent trends | ops | stdlib |
| `trust.py` | TrustRank for `$OV/wiki/` — Personalized PageRank with external anchor seeds, claim-level granularity, bi-temporal filtering, floor trust | B | stdlib |
| `snapshot_anchors.py` | Saves `url:` / `gist:` wiki anchors to Readwise and backfills the `readwise:` document ID so anchor evidence stays durable | B | `readwise` CLI |
| `lint.py` | Structural + corpus-level lint over `$OV/wiki/` — parse errors, duplicate titles, slug drift, orphan entries, graph topology | D | stdlib |
| `harness_lint.py` | Claude Code and Codex portability lint — root instructions, model profiles, capability mappings, command and agent registries | ops | stdlib |
| `harness_smoke.py` | Smoke runner: the ordered check list; the checks live in `smoke_common.py` (runner, `expect`, paths), `smoke_harness.py`, `smoke_semantic.py`, `smoke_vault.py`, `smoke_autoevo.py`, `smoke_routines.py`, `smoke_context.py`, `smoke_regressions.py` | ops | stdlib |
| `atelier_runtime.py` | Native runtime selector: resolves the Codex or Claude default, persists a gitignored local preference, and launches registered workflows without generating adapter prompts | ops | stdlib |
| `intent_coverage.py` | `/hi` and `$hi` intent catalog projection (`catalog`), per-route ledger (`intent-log`), and unrouted-request review (`intent-misses`) | ops | stdlib |
| `privacy_check.py` | Scans public-bound pathnames, worktree files, and divergent staged blobs for private vault titles plus exact local terms from gitignored `profile/private_terms.txt`; deliberate public opt-outs live in `privacy_allowlist.txt`; wired into `/lint` Phase 0c | ops | stdlib |
| `zk_audit.py` | Post-ingestion hygiene audit for `$OV/`: missing READMEs, raw-without-digest, archive↔working overlap, root orphans, suspicious dirs; wired into `/lint` Phase 0b | ops | stdlib |
| `staleness.py` | L2 staleness scoring — surfaces dormant, stale, and promotion-candidate notes | D | stdlib |
| `aggregate_freshness.py` | Aggregate-vs-detail staleness guard — flags aggregate trackers whose `Last updated:` is older than their newest subject file; `--discover` walks `freshness: required` frontmatter | ops | stdlib |
| `auto_memory_audit.py` | Audit pass over Claude Code auto-memory — surfaces stale, orphaned, dead-linked, or self-flagged provisional entries for human invalidation | ops | stdlib |
| `people.py` | Canonical person-note lookup by name fragment — pathlib walk (no xargs word-splitting); opt-in body-field matching via env var | ops | stdlib |
| `cues.py` | Unified quiet-by-default cue checker for Claude `/hi` and Codex `$hi` session start; silent when nothing fires and runtime-native command syntax when a cue is due | ops | stdlib |
| `render_runtime_edges.py` | Renders per-runtime edge files (Codex agent TOMLs, `$command` skills) from the registries; `--check` fails on any byte drift | ops | stdlib |
| `recurring.py` | Manages recurring obligations in `$OV/gtd/recurring.md` — re-emerging tasks with `every:` / `last-done:` due computation, distinct from one-shot GTD items | ops | stdlib |
| `todos.py` | Aggregate open TODOs from `$OV/gtd/` and reflection Next Action sections; computes priority from `due:` / `priority:` / age; flags closure candidates from daily-note language; subcommands `list`, `stale`, `closure-candidates`, `digest` — `digest` powers `/daily-reflection` Step 0 (reached via `/hi`) | ops | stdlib |
| `retrospect.py` | Draws an old note at random from `reflections/`, `wiki/`, `research/`, and `daily-notes/` to resurface forgotten material; deliberately not semantic search, biased away from the recent, with a cooldown so it cannot repeat and a fail-closed denylist because the draw leaves the machine in an email | ops | stdlib |
| `find_python.sh` | Prints an absolute path to a Python >= 3.11 for routine prompts and smokes; inside the routine sandbox a bare `python3` resolves to macOS 3.9 and fails on `tomllib`, and `uv run` cannot write `~/.cache/uv` | ops | stdlib |
| `routine_digest.py` | Digest CLI (`collect` / `render` / `write` / `ack` / `mail` / `report`) and the facade that re-exports the pipeline: `routine_digest_core.py` (registries, windows, markdown and manifest helpers), `routine_collect.py` (collection, updates, health, context), `routine_render.py` (mail-safe HTML), `routine_mail.py` (SMTP delivery) | ops | stdlib |
| `interests.py` | Consumption-driven interest ledger under `$OV/_meta/interests.toml`: events (watched, attended, read, played, completed, declared) decay with a 90-day half-life into a strength that sets `active` / `watch` / `dormant`; `ingest` pulls the structured sources (AniList tracking cache, the live-events log, Readwise book highlights); `evidence` curates what needs a reading (unattributed games, diary lines that may describe consumption) for the orchestrator, which records its judgment with `add`; `active --json` is what the culture and concert routines search. Protocol: `protocols/interest-discovery.md` | ops | stdlib |
| `daily_context.py` | Masthead context for the daily digest: each coding harness's remaining weekly window read from files it already leaves behind (claude-hud usage snapshot, Codex session `rate_limits` events) with a countdown to reset and the snapshot age, plus the day's forecast from Open-Meteo for a `--place` the interactive procedure takes from the calendar, falling back to `[weather] place` in the private `$OV/_meta/digest.toml` for unattended runs. Quota never touches the network; weather is the only fetch and degrades to a warning | ops | stdlib |
| `daily_brief.py` | Assembles the digest's first screen from `deadlines.py`, `recurring.py`, `todos.py`, `cues.py`, and an optional reminder cache declared in `$OV/_meta/brief_sources.toml`; triages by forfeitability, folds long-overdue items to counts, and enforces a hard line cap that never folds forfeitable items | ops | stdlib |
| `refresh_tracking.py` | Refreshes the derived daily-brief reminder cache from exact same-day AniList schedules, explicitly configured anime follow-ups, and the existing concert ticket-sale cache; preserves last-success data on source failure and never invokes a model | ops | stdlib, AniList API |
| `tracking_refresh_runner.sh` | Owner-gated, timeout-bounded deterministic runner for `refresh_tracking.py`; scheduled independently from the digest routine | ops | stdlib |
| `deadlines.py` | Read-only view over `$OV/_meta/deadlines.toml`: dated obligations (expiring perks, open windows, tickets) extracted weekly from prose trackers, each row requiring `<path>:<line>` provenance; freshness-gated so a stale index reports staleness instead of passing as current; subcommands `list`, `due`, `lint` | ops | stdlib |
| `session_log.py` | Session event log skeleton generator — handles late-sleep date rule and collision auto-increment | E | stdlib |
| `session_replay.py` | Opt-in private native-transcript capture with prompt journaling, secret screening, and activation-aware inspection; disabled by default, enabled by machine-local preference or process override | ops | stdlib |
| `context_bundle.py` | Builds route-first, byte-bounded profile and continuity projections from the intent registry | E | stdlib |
| `signal_facts.py` | Validates and ingests immutable private signal facts, derives definition-bound metrics, and emits bounded relevance-gated analysis bundles | ops | stdlib |
| `shadow.py` | Cross-provider shadow-log correlation + reporting — `group-start` / `group-close` witnesses for multi-leg call sites, `report` over the JSONL call logs | ops | stdlib |
| `command_timeout.py` | Runs one scheduled subprocess with an epoch-based wall-clock timeout that survives macOS sleep and terminates its process group on expiry | ops | stdlib |
| `_git.py` | One git subprocess wrapper (`run_git`, `git_paths`, `merge_state`, bot identity) shared by every script that shells out to git | ops | stdlib |
| `decisions.py` | Human-decision ledger at `$OV/_meta/decisions.jsonl`: `record` (reason mandatory), `import-autoevo` backfill, `list`, `stats` (per-class verdicts and precedent accuracy) | ops | stdlib |
| `precedent.py` | Precedent judge: nearest past decisions for a new item, gated model verdict, and `autoevo` defaults on the pending queue | ops | stdlib, `chat_completion.py` |
| `autoevo_preflight.py` | Checks autoevo Git, session, privacy, semantic, branch, and LFS readiness before model launch; writes a checksum-owned blocker audit without repairing Git | ops | stdlib, `git`, `uv`, optional Git LFS |
| `autoevo_pending.py` | Sole writer for the autoevo pending queue: append with peers dedupe, resolve, defer, auto-dismiss; atomic TOML writes that preserve unknown tables | ops | stdlib |
| `autoevo_quarantine.py` | Lists active per-scope quarantines for a selected cycle date, updates their state, and inserts generated skip evidence into the latest audit's Skipped section | ops | stdlib |
| `autoevo_verify.py` | Proves one autoevo cycle delivered a committed clean audit with real Forgetter coverage, one same-commit decay report per returned envelope, lint, claim-owned event journal, and matching final verification evidence | ops | stdlib, `git` |
| `routine_owner.py` | Claims and transfers the single machine allowed to execute local routines; compares a gitignored machine identity with shared `$OV/_meta/routine_owner.toml` | ops | stdlib |
| `routine_claim.py` | Validates calendar cycle IDs and atomically replaces canonical local-routine claim TOML; gives the runner a cheap completed/fenced/deferred schedule decision | ops | stdlib |
| `routine_result.py` | Validates a structured local-routine result against the declared output path, glob, size, and cycle claim time | ops | stdlib |
| `routine_lock.py` | Coordinates scheduled routines through atomic machine-local or owner-cycle reservations and DynamoDB; guarded recovery reconciles remote locks and synchronized claims | ops | `boto3` only for DynamoDB |
| `routine_audit.py` | Validates private routine support declarations against public local/cloud capability profiles; enforces command and Atelier-access bindings; checks fixed Codex availability, owner, CLIs, plugins, plists, evidence fingerprints, and loaded launchd jobs | ops | stdlib, optional local CLIs/plugins declared by profiles |
| `routine_cloud_bundle.py` | Builds a private ChatGPT Scheduled migration manifest and cloud-adapted prompt bundle under `$OV`; rejects public-repo and other non-vault targets | ops | stdlib |
| `routine_prompt_guard.py` | Validates the local-adapter preamble and rejects archived private routine prompts containing literal credentials before a headless model starts | ops | stdlib |
| `routine_profile_smoke.sh` | Runs a no-side-effect Codex invocation through one routine's exact local sandbox, web, and user-config envelope; records runtime evidence without exercising Gmail, Readwise, or workflow mutations | ops | `codex` CLI, routine audit helpers |
| `routine_permission_smoke.sh` | Runs an explicitly authorized, launchd-only Gmail-read or idempotent Readwise-write probe through a routine's exact Codex profile; records fresh external-permission evidence without mailbox-content access | ops | `codex` CLI, configured Gmail plugin or `readwise` CLI |
| `routine_runner.sh` | launchd wrapper for scheduled routines: owner-generation gate, wake catch-up cycle selection, command-bound capability and autoevo preflight, wake assertion, least-privilege Codex profile, lock, fingerprinted claim, fixed headless Codex execution, artifact validation, release, and autoevo post-run verification | ops | `codex` CLI, `caffeinate`, `routine_audit.py`, `routine_owner.py`, `routine_lock.py`, `routine_result.py` |
| `rewrite_paths.py` | Mechanical half of a tier rename — rewrites `$OV/<old>` to `$OV/<new>` across committed docs after editing `harness/paths.toml` | ops | stdlib |
| `relink.py` | Fixes broken markdown links after file moves — rewrites refs to each filename stem's current location (`--dry-run` / `--apply`) | ops | stdlib |
| `fission.py` | Generic directory fission per the 32-entry rule — splits a directory's .md children into bucket subdirs (first-letter, year-month, year/month); pair with `relink.py` | ops | stdlib |
| `wikilink_to_md.py` | Converts Obsidian wikilinks to standard markdown links — aliases, headings, date links, image embeds; unresolved links become semantic tags | ops | stdlib |
| `log_backlinks.py` | Retrofits `[[YYYY-MM-DD]]` wikilinks into markdown-table date cells so rows backlink the daily note for that date | ops | stdlib |
| `review.sh` | External reviewer wrapper (codex + direct-api leg in parallel; `gemini` kept as a legacy mode) for system-evolution diffs | ops | `codex` CLI, direct-api binding (`chat_completion.py`); `gemini` CLI optional |
| `chat_completion.py` | Stdlib-only OpenAI-compatible chat completion invoker. Stateless (one-shot) by default; `--session FILE` for multi-turn (history replayed each call). Selects the model via `--model <identity>` (schema in `harness/models.toml`, bindings in gitignored `profile/models.toml`) or direct flags. Backs the direct-api leg of every dual-voice role; switching providers is a binding edit, not a script change. `--max-tokens 0` omits the cap entirely (system-review path uses this). Every call (success and error) logs one JSONL line to `~/.cache/atelier/llm_calls/<date>.jsonl` for after-the-fact quality / latency / reasoning audit; pass `--no-log` to skip on sensitive prompts | ops | stdlib |
| `pricing.py` | Provider pricing catalog reader + cost calculator. Reads `scripts/pricing.toml` (flagship + standard per provider, USD per 1M tokens). Subcommands: `list` (sorted blended-cost table), `blended <provider> <class>`, `cost <provider> <class> --input N --output N`, `cost-from-log` (retrospective cost from `~/.cache/atelier/llm_calls/`). Used to drive future Pareto-optimal model selection (perf/cost) | ops | stdlib |

## Portable Harness

Claude commands under `.claude/commands/` and Codex skills under
`.agents/skills/` are the native runtime edges. Run `scripts/harness_smoke.py`
after harness edits to verify those mappings and lifecycle hooks without
touching `$OV/`.

`scripts/atelier_runtime.py` is optional for direct interactive use. It ships
with Codex selected, launches `$<command>` or `/<command>` unchanged, and lets
the user persist Claude with `python3 scripts/atelier_runtime.py use claude`.

## Public-repo privacy gate

Keep identity, goals, locations, account labels, schedules, and preference
policy under gitignored `$OV/` or `profile/`. Add exact literals that cannot be
inferred from vault filenames to `profile/private_terms.txt`, one per line;
single-word compatibility entries may remain in `profile/private_slugs.txt`.
Neither file is committed.

Before a public-bound commit, run `uv run scripts/privacy_check.py --json`.
The scanner checks tracked and untracked public-bound paths and files plus any
divergent staged blobs. Its JSON reports a coverage warning when the local
exact-term sidecar is absent; a clean hit count does not erase that warning.
Then run `$system-review` for the semantic privacy guard, which looks for
contextual identity, financial, demographic, schedule, and taxonomy leaks that
exact matching cannot recognize. These gates protect new commits; they do not
remove content from existing Git history or remote forks.

## `trust.py` — quick reference

Walks `$OV/wiki/*.md`, parses each wiki entry into claims and markers, builds a directed trust graph, runs Personalized PageRank, applies the claim-level floor trust of 0.1, and reports per-claim and per-note scores.

```
scripts/trust.py                                   # default table over $OV/wiki/
scripts/trust.py --note "$OV"/wiki/<file>.md       # per-claim breakdown
scripts/trust.py --as-of 2025-06-01                # bi-temporal snapshot
scripts/trust.py --json                            # structured output for /lint
```

**Model.** External `@anchor` markers are the only seeds of trust. `@cite` edges propagate trust from cited claims to citing claims. `@pass` markers never accumulate trust; a reviewer-verified pass only enables the claim-level floor of 0.1 on a structurally-valid note.

**Determinism.** Pure Python, stdlib-only. PageRank is a direct power-iteration implementation matching `networkx.pagerank(G, personalization=anchor_dict)` semantics (dangling mass redistributes to the personalization vector, damping 0.85, tolerance 1e-9, max 200 iterations). Zero new dependencies.

**Bi-temporal.** Every marker has `valid_at` (required); optional `invalid_at`. `--as-of` filters markers by the active window. With no active anchors in the snapshot, all claim scores are 0 by design: TrustRank with an empty seed set means no trust has entered the graph.

**Structural integrity.** `trust.py` enforces items 1 to 10 of `protocols/wiki-schema.md` § Structural Integrity Check. A note that fails parse contributes no seeds and no propagation edges, but its claims still appear in the report with score 0 and a `fail` status. Corpus-level lint (items 11 to 15) is implemented by `scripts/lint.py`, surfaced through `/lint`.

**Exit codes.** `0` on success. `2` on usage error (missing file, invalid date, note outside `$OV/wiki/`).

See `protocols/wiki-schema.md` for the schema and the trust-model rationale.

## Conventions

- **Exit-code convention for JSON-emitting scripts**: `0` success, `1` a
  reported failure the caller can act on (a refused op, a verdict of "not
  verified"), `2` usage, input, or data errors and any unforeseen exception,
  always with a single `{"error": "..."}` object on stdout. `harness_smoke.py`
  and the routine runner branch on these.
- **Shared helpers**: `_paths.py` (registry paths, `atomic_write`,
  `parse_iso_date`, `retry_transient`) and `_git.py` (git subprocess). New
  scripts import these instead of re-implementing them.
