# Local-First Architecture

The user's knowledge layer is plain-text Markdown files under `$OV/`. `$OV` is an environment variable that each user sets to their own vault root. The system reads and writes these files directly; there is no remote note-store mirror.

This is the prerequisite for everything in `wiki-schema.md` and `epistemic-hygiene.md`. Trust propagation, claim-level granularity, bi-temporal anchors, and structural-integrity linting all require deterministic Python access to plain-text files, which this layout provides.

## The Layers

The model has **five layers (L1–L5), numbered by depth of crystallization.** Higher number = higher trust. The axis is not provenance (human vs AI) but storage/certification depth: how much structural work, anchoring, and peer verification a note has accumulated by virtue of where it lives.

(Note: "Layer" here refers to the L1–L5 knowledge-storage axis. A separate orthogonal axis — the **validation-depth taxonomy** in `epistemic-hygiene.md` — uses the names *alloy* / *wiki entry* / `#solo-flight` for what a note *is*. Do not conflate the two: L1–L5 is *where*, alloy/wiki/solo-flight is *what*.)

```
    L5 — Foundation                   (reserved — textbook-level, universally certified)
    ────────────────────────────
    L4 — Locally certified            <paths.wiki>/
          authoritative knowledge     anchored, schema-validated, TrustRank-scored
    ────────────────────────────
    L3 — Externally certified         <paths.papers>/, <paths.preprints>/
          peer-reviewed or high-citation receipts
    ────────────────────────────
    L2 — Working / half-baked         <paths.daily_notes>/, <paths.reflections>/,
          alloy by default            <paths.research>/, <paths.agent_findings>/,
                                      <paths.wip>/, <paths.gtd>/
    ────────────────────────────
    L1: Raw capture                  Readwise, <paths.inbox>/, <domain>/raw/,
          fast, sloppy, ephemeral     <paths.cache>/
```

Promotion is **opportunistic and upward:** L1 capture crystallizes into an L2 draft or reflection; a recurring L2 thought earns an L4 wiki entry once it has anchors and claims; L3 receipts flow in from scout fetches and Readwise curation. There is no demotion workflow — invalidation is additive (bi-temporal markers in wiki entries), not destructive.

### L1 — Raw capture

The fast, sloppy, ephemeral layer. Readwise's inbox (cloud-only, accessed via the `readwise` CLI; no local mirror) holds external content. `<paths.inbox>/` holds pending local captures, `<domain>/raw/` holds durable source artifacts, and `<paths.cache>/` holds disposable fetches and derived files. No guarantees about structure across this layer. Promotion upward is opportunistic.

### L2 — Working / half-baked

The alloy layer. Most of the user's active thinking lives here: daily free-writes (`<paths.daily_notes>/`), session reflections (`<paths.reflections>/`), user-initiated research reports (`<paths.research>/`), promoted agent synthesis briefs (`<paths.agent_findings>/`), working drafts (`<paths.wip>/`), and active planning (`<paths.gtd>/`). Alloy by default; the validation-depth taxonomy lives in `protocols/epistemic-hygiene.md`. Fully searchable, citable, but not certified. The substrate from which wiki entries are distilled.

Older topic directories (career, research, people, etc.) carried over from earlier knowledge systems are parked in `<paths.archive>/` and stay there until individual notes are surfaced upward.

**Transient capture inbox.** `<paths.zettelm>/` is a git submodule (synced from a mobile capture app) that holds short-lived, self-authored content awaiting digest: date-stamped narratives, ad-hoc thoughts, voice recordings, photos, PDFs. It is not its own tier — contents are enriched and routed by `/sync` into the appropriate persistent L2 destinations (`daily-notes/YYYY/MM/<date>.md` for narrative, `<domain>/raw/` for attachments) and then deleted from zettelm. Nothing should be expected to survive long-term inside `zettelm/`.

### L3 — Externally certified

Peer-reviewed papers, high-citation work, and curated reading corpus. Lives in `<paths.papers>/` (local PDFs and reading artifacts) and `<paths.preprints>/`. Preprints sit at L3 (not L2) because each file in `<paths.preprints>/` is a structured paper-review artifact (unofficial review of an arXiv paper, conference scrape with citations) — externally anchored, not a working free-write. The canonical id for papers is `s2:` / `arxiv:` / `doi:`; for articles, `url:` or a Readwise document id (when fetched via CLI). L3 receipts are the anchor points for L4 wiki claims — an `@anchor` marker in a wiki entry points at an L3 receipt.

The teaching doc that explains how agents query the papers directory lives at `sources/local-papers.md` (an execution-layer doc in the atelier repo).

### L4 — Locally certified (wiki)

The slow, structured, authoritative layer. Lives in plain Markdown files under `<paths.wiki>/`. Each file follows `wiki-schema.md`. Each file is parseable by `scripts/trust.py` and produces a per-note trust score. Cross-references between wiki entries are `@cite` markers, which become edges in the trust graph.

**Directory is the certification.** A note is a wiki entry by virtue of living under `<paths.wiki>/`. There is no `#compiled-truth` or `#wiki` tag; the trust engine walks the directory and treats every file inside it as a wiki entry. The rest of `$OV/` stays alloy by default — the trust engine does not touch it. This gives the trust engine a single, fast directory traversal as its working set and avoids tag-collision with the user's existing tagging conventions.

L4 is the only tier where:

- Trust propagation runs.
- Bi-temporal anchors are tracked.
- Structural-integrity lint applies.

### L5 — Foundation (reserved)

Universally certified knowledge — textbook-level material that the user considers settled. No folder yet; the tier exists for future use when there is enough material to warrant one.

## Project Layout

Two roots:

- **System layer** — the `atelier/` repo (this directory). Orchestrator config, agents, protocols, scripts, source-handling teaching docs, and `sources/cite.py`. Version-controlled; no personal data.
- **Vault layer:** the user's note root, addressed as `$OV/`. A flat set of tier-labeled directories: `wiki/` (L4), `papers/` / `preprints/` (L3), `daily-notes/` / `reflections/` / `research/` / `agent-findings/` / `wip/` / `gtd/` / `<domain>/` (L2; see `harness/paths.toml` for the canonical list), `inbox/` and `<domain>/raw/` (L1 provenance), `cache/` (L1 derived and ephemeral), `sessions/` (process records), and `archive/` (parked notes). Readwise inbox is L1 but cloud-only and is queried explicitly.

Vault paths use `$OV/` (e.g., `<paths.wiki>/`, `<paths.papers>/`); each user sets `$OV` to their note root (typical: `export OV="$HOME/notes"`). Repo-internal paths (`scripts/`, `protocols/`, `sources/cite.py`, `frameworks/`) stay project-relative and require no env var. The vault may live anywhere on disk (Google Drive, iCloud, a plain local folder); the system only needs `$OV` to point at it.

## Directory Layout (canonical)

```
atelier/                           (system root — the agent code)
├── CLAUDE.md                       # orchestrator instructions
├── .claude/agents/                 # team definitions
├── .claude/commands/               # slash commands
├── protocols/                      # system protocols (this directory)
├── frameworks/                     # thinking frameworks
├── profile/                        # gitignored config: self-model + private preferences (identity, directions, expertise, diet, reader_persona, credentials-index, private_slugs, examples, research-profile)
├── scripts/                        # Python tooling (trust.py, lint.py, semantic.py, ...)
└── sources/                        # source-handling teaching docs and helpers
    ├── cite.py                     # academic citation helper
    ├── readwise.md                 # Readwise CLI teaching doc
    ├── scholar.md                  # Semantic Scholar teaching doc
    └── local-papers.md             # local papers teaching doc

$OV/                                (vault root — set via env var)
├── wiki/                           # L4 — locally certified (authoritative)
│   ├── <topic-1>.md                # each file follows wiki-schema.md
│   └── <topic-2>.md
├── papers/                         # L3 — peer-reviewed / high-citation papers
├── preprints/                      # L3 — arxiv + paper reviews
├── daily-notes/                    # L2 — daily free-writes (user-authored)
├── reflections/                    # L2 — session reflection files
├── research/                       # L2 — user-initiated research reports
├── agent-findings/                 # L2 — promoted scout briefs and agent synthesis
├── wip/                            # L2 — work-in-progress, long-form drafts
├── gtd/                            # L2 — active planning (year goals, trackers)
├── <domain>/                       # L2 — additional domain surfaces; canonical
│                                   #      list lives in harness/paths.toml
├── cache/                          # L1 — ephemeral raw web fetches and snapshots
├── zettelm/                        # transient capture submodule (digested by /sync, then cleared)
└── archive/                        # parked notes (surfaced opportunistically)
```

The trust engine and the wiki schema only see the `<paths.wiki>/` subtree. Everything else is alloy or receipts and the trust engine does not touch it.

## Search Projections

Storage layers and search scopes are related but not identical. The physical
vault remains the source of truth; the semantic index is a machine-local
projection with deliberate visibility boundaries:

- `active` is the default. It includes current authored knowledge and compact
  locator cards for raw clusters.
- `raw`, `archive`, `inbox`, and `process` are explicit deep-search scopes.
  `raw` includes readable raw text; `process` maps to `<paths.sessions>/`.
- `all` is an audit or recall-maximizing union, not the agent default.
- Any nested `cache/`, `_meta/`, `_routine_prompts/`, `.trash/`, or `_tools/`
  directory never enters semantic retrieval.
- Readwise remains external and opt-in through `--sources local,readwise`.

`scripts/semantic_corpus.py` owns classification, hard exclusions, locator
generation, and duplicate accounting. Stub search, real indexing, freshness,
audits, and tests must consume that policy rather than recreate directory
rules. The CLI and operational details live in `sources/semantic.md`.

## Source of Truth

`$OV/` is the canonical copy of the user's knowledge layer **for L2-L4 content that has been durably written and confirmed present on disk**. Two declared carve-outs apply (Readwise as L1 SOT; in-flight routine outputs as provisional SOT in the claude.ai session log). Full SOT scope, carve-out rationale, and recovery paths: `backend-taxonomy.md` § SOT Scope.

Persistence has three concerns, each handled by a different mechanism:

- **Device sync**: handled by whatever filesystem $OV lives on (Google Drive, iCloud, plain folder). Outside the system's concern.
- **Version control**: $OV may be a git repo with an optional remote (typical: a private GitHub repo); see `protocols/repo-conventions.md` for the on-disk conventions that make `$OV` render correctly through GitHub. The system does NOT auto-commit or auto-push; commits happen via user-driven git operations or via `/sync` for the zettelm submodule.
- **Mobile-capture submodule (`zettelm`)**: a git submodule with its own remote, pushed by `/sync` after each digest. See `.claude/commands/sync.md` § "Stage and commit zettelm deletions" for the push flow. The remote URL is user-private (lives in the submodule's `.git/config`).

There is no two-way sync between $OV and any other layer, and no idempotency ledger. The system reads/writes $OV directly; whatever the user has configured for backup happens transparently underneath.

Daily notes (under `<paths.daily_notes>/`) are user-authored. By default the system reads them; it does not write to them. Curator dispatches that target a daily-note path are refused. **Exception (cloud-native capture):** when the user dictates raw daily-note content through chat, the Scribe agent (`daily_note` operation) records it verbatim. This is the only path by which the system writes to a daily note; the orchestrator does not transcribe directly.

All other tiers can be written by the orchestrator after user approval. The Curator drafts proposals; the orchestrator owns `Write` and `Edit` and applies them to a `target_path` under `$OV/`.

## Migration Strategy: Opportunistic, Not Big-Bang

There is no bulk migration of existing notes into the wiki layer. Older topic directories that are no longer active sit in `<paths.archive>/`. The wiki layer grows organically:

- New wiki entries are written to `<paths.wiki>/` directly (Curator drafts; orchestrator writes after approval).
- Existing notes (`<paths.archive>/` or anywhere else in L1/L2) are surfaced to L4 **only when they are about to become anchors for a new wiki claim** — at that point the user (or Curator) extracts the relevant claims, structures them per the schema, writes the wiki entry, and the original note remains in place as an L1/L2 capture record (untouched).
- There is no goal to hoist the entire vault into the wiki layer. L1 and L2 remain the home for daily notes, session reflections, drafts, and most thinking. Most notes will never be in L4 — that is correct, not a failure.

The expected steady-state ratio is roughly: hundreds of L1/L2 notes for every L4 wiki entry. L4 is the slow, careful, anchored kernel. L1 and L2 are the fast surface.

## Aggregation vs. Detail (orthogonal to L1-L5)

L1-L5 measures certification depth, not aggregation. Within a single tier (typically L2), the user often maintains both *detail files* (one file per subject, e.g. `<paths.travel>/trips/<trip>.md`) and *aggregate trackers* (one file summarizing many subjects, e.g. `<paths.travel>/<calendar>.md`, `<paths.travel>/<inventory>.md`, or `<paths.finance>/<benefits-tracker>.md`). Aggregates are hand-mirrored views over the details; nothing pushes detail edits back to the aggregates.

The system handles this asymmetry at **read time**, not write time. Read workflows that surface aggregate values run `scripts/aggregate_freshness.py` as a pre-step and emit a divergence warning when any aggregate's `Last updated:` line lags the newest detail file. The convention: detail files are the SOT; aggregates may be stale; readers must cross-check the detail before quoting an aggregate value as authoritative. This is a read-time guard against antipattern #6 (shadow state); the divergence is made visible rather than silently propagated.

Write-time propagation (auto-generating aggregates from details) is deferred. It requires structured frontmatter on every detail file plus per-aggregate generators, which is heavier scaffolding than the read-time guard. The read-time guard is sufficient as long as readers respect the divergence warning.

**Self-declaring aggregates (discovery convention).** Aggregates opt in to the guard by adding a YAML frontmatter block at the top of the file:

```yaml
---
subjects: <path-to-subjects-dir>     # e.g. travel/trips/ (relative to $OV) or absolute
freshness: required                  # marks the file as an aggregate to check
---
```

With both keys present, `scripts/aggregate_freshness.py --discover` walks `$OV` (skipping `cache/`, `archive/`, `papers/`, `preprints/`, `zettelm/`, dotfiles), groups self-declared aggregates by their `subjects:` dir, and runs the same scan the explicit-args form does. `--stale-only` filters to just stale entries — silent when everything is fresh, ideal for session-start cues. The knowledge-and-retrieval rule in `CLAUDE.md` wires this into every read of an aggregate: before quoting an aggregate as authoritative, run `--discover --stale-only` and cross-check any flagged file's subject SOT.

Adoption is forward-looking: existing aggregates opt in by adding the frontmatter block. Files without the marker are silently ignored by `--discover`; the explicit `--subjects` / `--aggregates` form continues to work for ad-hoc or transitional cases.

See also: Planner vs Executor (below) for the analogous asymmetry between upstream planning files and downstream execution projects.

## Planner vs. Executor (orthogonal to L1-L5)

A second asymmetry, also typically within L2: a *planner file* enumerates intent (a checklist of actionables, perks to redeem, trips to book), and one or more *executor files* carry out individual items in depth (a trip-prep project, a visa-application project, a booking thread). The planner is the SOT for "what's outstanding"; the executor owns the working detail and the final receipts. Without a convention, closure happens in the executor and the planner silently rots — the inverse of the aggregate-vs-detail problem, but the same shadow-state pathology (antipattern #6).

The convention is to make the upstream link bidirectional at creation time and to require backfill at closure time:

- **Downstream declares upstream** — every executor project's frontmatter carries `upstream: <path>#<anchor>` pointing at the row/line in the planner that spawned it. This is set when the executor file is created, not retrofitted.
- **Closure form is a backfill receipt** — when the executor completes an item, the corresponding planner row is rewritten as `- [x] <task> → backfilled <upstream-path>#<row> @YYYY-MM-DD`. Status (done) and provenance (where the work landed) travel together.
- **Backfill happens in the same turn/commit as the close.** Closing the executor item without touching the planner is the bug; the two edits are a single atomic transaction.

This convention is forward-looking — existing planner/executor pairs are not retrofitted. It crystallized from two instances where downstream work completed but upstream planning files lagged: a `<paths.travel>/honeymoon` booking thread on 2026-05-16 and the `<paths.travel>`<paths.abroad>/<case>` project on 2026-05-17.

A deferred primitive — `scripts/handoff_backfill_check.py` — would scan executor files with `upstream:` frontmatter and flag closed items whose upstream row is still unchecked. Marked `[deferred]` until rule-of-three: one more instance and the script becomes worth writing.

## Per-Agent Contract

| Agent | L1/L2 working layer | L4 wiki (`<paths.wiki>/`) | L3 receipts |
|---|---|---|---|
| **Researcher** | Local `active` semantic search first, using context JSON with at most 10 capsules; selects `raw`, `archive`, `inbox`, or `process` only when required, then reads relevant sections from 3-5 files. `Grep` + `Read` remain structural. | Reads `<paths.wiki>/` directly when certified scope is required. | Reads selected `<paths.papers>/` and `<paths.preprints>/` receipts directly. |
| **Curator** | Drafts note proposals (compactions, merges, new notes, rewrites); the orchestrator writes after user approval (Curator has no `Write` tool). | Drafts wiki entries with `target_path: <paths.wiki>/<slug>.md`. The orchestrator writes the file after approval, then runs `scripts/trust.py --note <path>` to verify structural integrity and report initial scores. | Unchanged. |
| **Synthesizer** | Reads capture-layer briefs from Researcher; produces drafts the orchestrator writes to `<paths.reflections>/`. | Reads wiki trust scores when available to weight evidence. | Unchanged. |
| **Reviewer** | Continues to gate write-backs. Gates wiki writes as well. A `@pass: reviewer | status: verified` marker is added to a claim only after Reviewer signs off. | Unchanged. |
| **Scout** | Unchanged. | Unchanged. | Writes promoted briefs to `<paths.agent_findings>/` (not the ephemeral `<paths.cache>/`). |

## Cross-References

- Tag taxonomy and validation-depth principle: `epistemic-hygiene.md`
- Wiki entry format and trust propagation rule: `wiki-schema.md`
- Trust engine implementation: `scripts/trust.py`
- Lint integration: `.claude/commands/lint.md`
- Backend taxonomy and SOT carve-outs: `backend-taxonomy.md`
