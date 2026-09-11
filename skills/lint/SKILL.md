---
name: lint
description: Run harness, privacy, structural, and staleness checks.
---
# /lint — Structural + corpus-level checks over `<paths.wiki>/`

> Also reachable via `/hi <natural language>` (e.g., `/hi lint the wiki`, `/hi wiki audit`,
> `/hi wiki orphans`). See `harness/intents.toml` `[intents.lint]` for the row's example
> phrases. Both paths execute this same procedure.

Deterministic Python pass. The LLM never hand-checks structure — `scripts/lint.py` is the single source of truth, mirroring the `scripts/trust.py` pattern.

**Scope:** Three passes. (0) Harness portability, $OV ingestion hygiene, and privacy checks. (1) Structural: everything under `<paths.wiki>/`. (2) Staleness: L2 working-layer directories (`<paths.agent_findings>/`, `<paths.wip>/`, `<paths.gtd>/`, `<paths.preprints>/`, `<paths.reflections>/`, `<paths.research>/`). Structural lint enforces the wiki schema; staleness lint surfaces L2 notes that need attention (archival, compaction, or promotion to L4).

**What gets checked:**

| Check | Severity | Source |
|---|---|---|
| Per-note parse errors — items 1-10 of `protocols/wiki-schema.md`, plus dangling `@cite` targets (both surface under the `parse-error` code) | ERROR | `scripts/trust.py` parser + resolver |
| Duplicate titles across wiki entries (breaks `@cite` resolution) | ERROR | `scripts/lint.py` |
| Slug ↔ title alignment (filename stem matches H1 title) | WARN | `scripts/lint.py` |
| Orphan entry — no inbound `@cite` from any other wiki entry (trust cannot propagate to it) | WARN | `scripts/lint.py` graph topology |
| No outbound cite — entry does not `@cite` any other wiki entry | INFO | `scripts/lint.py` graph topology |
| Shared anchor, no cite — two entries reference the same `@anchor` but lack a `@cite` edge | INFO | `scripts/lint.py` graph topology |
| `url:` or `gist:` anchor missing `readwise:` field (`readwise-missing`) | WARN | `scripts/lint.py` — save to Readwise with `anchor-evidence` tag and backfill the document ID; fix via `uv run scripts/snapshot_anchors.py --apply --note "<paths.wiki>/<Title>.md"` |
| Technical term in claim body not in vocabulary allowlist and not matching any wiki entry title (`unfounded-term`) | INFO | `scripts/lint.py` — add term to `scripts/wiki_vocabulary.txt` if common knowledge, or add a wiki entry, or add a parenthetical definition inline |
| Localized shadow missing for a configured language (`shadow-missing`) | WARN | `scripts/lint.py` — run /promote Phase 4 or regenerate the shadow manually. Configured shadow paths live under `[paths.wiki_localized]` in `harness/paths.local.toml`. |
| Localized shadow older than English source (`shadow-stale`) | WARN | `scripts/lint.py` — re-translate the localized shadow to match the updated English source |
| Public configuration schema and Claude/Codex harness alignment (`registry-schema`, `registry-read`, `registry-validator`, source/reference/edge findings) | ERROR/WARN/INFO | `harness/registry.schema.json` + `scripts/harness_lint.py` |
| `$OV` ingestion hygiene (missing READMEs, raw-without-digest, archive↔working-tier overlap, root-level orphans, empty .md files, suspicious top-level dirs) | INFO (advisory) | `scripts/zk_audit.py` — see `protocols/drive-zk-ingestion.md` § Post-ingestion verification |
| Claim missing `^cn` block ID (`block-id-missing`, deferred — Phase D) | WARN | `scripts/lint.py` — regex `\^c[0-9]+$` on last line of each claim body; absent marker is a nudge, not a reject (per `protocols/wiki-schema.md` §"When `^cn` is recommended") |
| Non-`^cn` block ID inside a wiki entry (`block-id-violation`, deferred — Phase D) | ERROR | `scripts/lint.py` — any `^<token>` that does not match `\^c[0-9]+$` is a schema violation (no `^summary`, `^fig1`, `^revlog-*`, etc.) |

**Not checked:** cross-note `@anchor` date consistency. Per `protocols/wiki-schema.md`, `valid_at` is the day the marker was added to its home note, so the same source being anchored from two notes on different days is the normal case.

Exit code: 0 if no ERROR-level findings, 1 otherwise. WARN and INFO never fail the run.

## Process

### Phase 0: Harness health

```
Bash: python3 scripts/harness_lint.py --json
```

Read `counts` and `findings` (`severity`, `code`, `where`, `message`). Any ERROR
blocks the run until fixed; WARN and INFO are advisory. This pass owns the
CLAUDE.md size and bold-marker checks; do not repeat them manually.

`registry-schema` identifies the source file and failing field. A
`registry-validator` setup error requires installing the pinned dependencies
with `uv sync --locked` before retrying; lint itself never installs them.

```
Bash: uv run scripts/routine_digest.py check; echo "exit=$?"
```

Re-runs the rendered-digest invariants on the newest artifact under the digest
routine's output directory (countdown printed once per ledger row, tech feed
carrying Chinese notes, every decision card with a settling condition). Read
the exit: `3` means findings, and each `check:` line is reported as WARN,
because `write` printed the same lines the morning it happened and a finding
is one nobody acted on. `0` is clean. Any other nonzero exit is an execution
failure (unset `$OV`, a broken digest registry, an unreadable artifact) and
counts as ERROR like any other Phase 0 failure.

### Phase 0b: $OV ingestion hygiene audit

```
Bash: uv run scripts/zk_audit.py --json
```

Read `categories` and `total`; category rows identify `where` and `detail`.

Advisory only: never blocks the run. Exit code 0 unless $OV is missing (exit 2). Surface a one-line summary per non-empty category. Detailed listings are read on demand via `uv run scripts/zk_audit.py` (no `--json`). Source of truth: `protocols/drive-zk-ingestion.md` § Post-ingestion verification.

### Phase 0c: Privacy leak scan

```
Bash: uv run scripts/privacy_check.py --json
```

`--json` mode emits a document on every run regardless of exit code. Route on its `action` field: `"proceed"` → pass (WARN first on any `coverage_warnings`); `"soft_skip"` → note "privacy gate skipped (<reason>)" and continue; `"abort"` → ERROR, block and present each `hits` entry verbatim. No JSON / exit ≥ 2 without JSON → real script error: surface stderr, soft-skip.

Any non-empty `hits` array is an ERROR: each entry is a private identifier (filename stem, wikilink target, slug from `profile/private_slugs.txt`, or exact term from `profile/private_terms.txt`) that appears in a public-bound pathname, worktree file, or staged blob. Present each hit verbatim with its file, source, and line number. Remediation:

- Replace the private title with a generic placeholder (e.g., `Sample Wiki Entry`, `Topic A`).
- Add private names, places, program labels, and preference phrases that cannot be inferred from vault titles to gitignored `profile/private_terms.txt`, one exact term per line.
- Or, if the exposure is deliberate (e.g., the title is fully public and appears as an illustrative example), add the stem to `scripts/privacy_allowlist.txt` and document the rationale in the commit message.

The check is a blocking quality gate for any commit that touches tracked files when the gate ran meaningfully (no skip flag). Do not proceed to structural lint if Phase 0c returns hits.
An absent exact-term sidecar is a coverage warning, not proof of a leak. Keep the
semantic privacy round enabled and populate the gitignored sidecar before
claiming exact identity or preference coverage.

### Phase 1a: Structural lint

```
Bash: python3 scripts/lint.py --json
```

Read `wiki_dir`, `counts`, and `findings` using the Phase 0 finding fields.

### Phase 1b: Staleness lint

```
Bash: python3 scripts/staleness.py --json
```

Read `thresholds`, `counts`, and `notes` (`path`, `staleness`, `category`).

Staleness findings are always advisory (no ERROR level). They surface L2 notes that have gone cold, using the formula `days_since_modified / (1 + log(1 + reference_count))`. Notes referenced from wiki entries or recent reflections decay slower.

### Phase 2: Present

Group findings by severity. If the corpus is clean, say so and stop.

For each ERROR-level finding: show the code, file path, and message verbatim. Do not rephrase the message — `scripts/trust.py` and `scripts/lint.py` emit precise line numbers that the user needs.

For WARN-level findings: show them but mark them as non-blocking.

For INFO-level findings: roll them up into a one-line summary (e.g., "4 entries with no outbound `@cite`: consider adding cross-references") unless the user asks for the full list.

**Staleness section** (from Phase 1b): present after the structural findings, under a separate heading. Group by category:
- **stale** notes: list paths, suggest archiving to `<paths.archive>/`
- **dormant** notes: list paths, suggest review or compaction
- **promote** candidates: list paths, suggest `/promote` to create L4 wiki entries
- If all notes are active, say so in one line and move on.

### Phase 3: Offer fixes

For each fixable category, ask the user before acting:

| Finding code | Fix | How |
|---|---|---|
| `slug-mismatch` | Rename file or edit H1 | Ask the user which side to change. Never rename without confirmation — downstream `@cite` targets key off the title. |
| `parse-error` (e.g., missing `valid_at`, non-sequential `[Cn]`) | Edit the wiki entry | Route to the user; do not auto-edit wiki entries. |
| `duplicate-title` | Edit one of the H1 titles | Ask the user which note keeps the title. |
| `dangling-cite` | Fix the `@cite` target or remove the marker | Surfaced under `parse-error` code (trust.py's resolver appends to `parse_errors`). Route to the user. |
| `orphan-entry` | Add `@cite` markers from related entries | Suggest specific claims in other entries that could cite the orphan. The `shared-anchor-no-cite` findings often point to the right pairs. |
| `shared-anchor-no-cite` | Add `@cite` between the pair | Show the shared anchor and suggest which direction the cite should flow (from the more general to the more specific claim). |

### Phase 4: Rerun (if fixes applied)

If any user-driven fixes were made, suggest rerunning `/lint` manually to confirm the state.

## Rules

1. **Never hand-check structure.** Always call `scripts/lint.py`. If the script is missing or errors, fix the script — do not re-derive the checks in the LLM.
2. **Ask before fixing.** All fixes route back to the user; `scripts/lint.py` is advisory-only.
3. **ERROR findings block further wiki work.** If a wiki entry has a parse error, do not cite it from new entries, do not score it — fix it first.
4. **WARN findings are advisory.** The system keeps working; the user decides whether to act.
