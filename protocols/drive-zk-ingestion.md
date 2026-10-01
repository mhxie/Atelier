# Drive → $OV Ingestion Protocol

## Roles

- **Google Drive top-level domain folders** (`~/Google Drive/My Drive/<Domain>/`) and **`~/Downloads/`** are the **raw landing zones**. New files arrive here from web downloads, exports (e.g., MyChart), scans, photo dumps, manual uploads.
- **`$OV/` (the vault, a Git work tree)** is the **structured repository** — single source of truth for organized data.
- After ingestion, originals live only in `$OV/<domain>/raw/`, a link that keeps them in the raw store and out of Git (or in the gitignored cache for entities not yet promoted). Drive top-level domain folders and Downloads are transient.

This protocol governs the flow from landing zone → repository.

## Default mode: mv

Default is **mv** (move), not cp (copy). Goal: `$OV` is single source of truth, Drive top-level / Downloads do not duplicate.

### Exceptions where cp is correct

- **Format-conversion**: when the digested form is lossy (e.g., HEIC → jpg, DOCX → txt, video → transcript). mv the original into `$OV/<domain>/raw/`, keep the transcoded version alongside as the markdown-friendly companion.
- **Uncertain completeness**: if extraction did not capture everything (e.g., a viewer skin / browser package alongside a data PDF), mv the auxiliary files into a sibling folder (`raw/<event>-viewer/`) rather than delete.
- **Explicit user request to dual-keep**: the user may say "keep the Drive copy" for a specific transition.

When uncertain → mv into `$OV` (don't delete). `$OV` is the destination either way.

## Two-track rule: low-value vs high-value

After mv into `$OV`, classify each file by future utility — this determines destination subtree and whether to write a digest:

### Low-value → `<paths.archive>/<category>/`

Pure backup, no digest, no expectation of revisit. Categories follow existing archive structure (`career/`, `courses/`, `practical/`, `people/`, etc.). Examples:
- Old insurance applications past their effective date
- Single-bill receipts and payment confirmations
- Boilerplate compliance notices (privacy notifications, ToS confirmations)
- Past calendar invites for fulfilled appointments
- Coursework / academic artifacts past their decision window
- Application materials for closed processes

### High-value → working tier (`$OV/<domain>/raw/`) **+** digest in working tier

Two artifacts together:
1. **Raw**: mv original to `$OV/<domain>/raw/` for provenance and full context.
2. **Digest**: write a markdown extract in the working tier (`$OV/<domain>/reports/`, `$OV/<domain>/<topic>.md`) capturing the retrieval-relevant essence — key dates, structured data, cross-links, decision implications.

Rationale: without a digest, valuable raw rots unread in storage; without raw, the digest can drift from source. Both serve different purposes.

Examples:
- Lab results, scan reports, visit summaries → digest extracts findings, references raw PDF
- Lease contracts → digest captures rent / term / parties, references raw lease PDF
- Technical / strategic documents you will reference again → digest captures decision-relevant essence

### Triage decision

Ask: *would future-me grep for this file's content?*
- Yes → high-value, raw + digest.
- No, but could matter for an edge case (audit, dispute) → low-value, archive raw only.
- No → consider whether to keep at all (some files truly belong in `<paths.cache>/` or trash).

Don't dump raw directly into working tier subdirectories (`reports/`, `research/`). The working tier is for digests. Raw stays in `raw/` siblings.

### Lifecycle: closed domains demote to archive

When a domain is no longer an active phase of life (e.g., a degree program finished, a prior employer fully off-boarded with no remaining process tails, a retired hobby), `mv` the entire working-tier directory to `<paths.archive>/<domain>/`. This preserves digests (README, timeline.md) as historical snapshots while removing the implicit "active reference" status.

Example: a working `education/` surface migrates to `<paths.archive>/education/` after PhD completion + start of full-time work. The raw + digests come along intact.

Don't pre-archive: a domain stays in working tier as long as it has live followups, ongoing decisions, or recent file additions.

## Domain routing

At ingestion the agent routes per content semantics, the source folder name, and the existing `$OV/` directory structure (canonical L2 layout: `protocols/local-first-architecture.md`). For most domains the destination is obvious from the Drive folder name plus the file content; let the agent decide.

A few aliases are not derivable from folder name alone and are committed here so independent runs converge on the same destination:

| Drive top-level | $OV subdirectory |
|---|---|
| `Medical/` | `<paths.health>/` |
| `Immigration/` | `<paths.abroad>/` |

Anything more specific (per-document subfolder conventions, archival rules, document-type taxonomies) belongs in the per-domain `$OV/<domain>/README.md` (gitignored, user-private), not in this protocol.

`~/Downloads/<domain>/` content is transient by nature; route per content to the appropriate `$OV/` domain and `mv` eagerly.

## Ingestion workflow

For each ingestion task (one domain or one batch):

0. **Rescan the landing zone before concluding that a file is absent.** Source
   inventories are point-in-time. If the user downloads or exports a file
   during the active session, discard an earlier negative scan and survey
   Downloads / Drive again after the next user message. A request to import a
   named landing-zone file authorizes the in-scope move and digest; it does not
   authorize unrelated Downloads cleanup.
1. **Survey** the source folder. Inventory: what's there, natural groupings (per-event subfolders, loose files, photos).
2. **Tidy at source** if needed. Group orphaned loose files into appropriate subfolders before mv. (Easier to fix structure at source than after relocation.)
3. **Create the destination** under `<domain>/raw/` in the raw store if absent, then run `uv run scripts/zk_audit.py --fix-links`. Preserve source subfolder structure where it makes sense.
4. **mv files** in. Use `mv -- "$src" "$dst"` to handle filenames with leading dashes / spaces.
5. **Extract structured data** into markdown in `$OV/<domain>/` (not under `raw/`). Naming convention: `<YYYY-MM-DD>-<slug>.md` for events, `<topic>.md` for cross-cutting indexes. Cross-link related files using `[[wikilink]]`.
6. **Cross-link source raw**: structured markdown should reference its raw source (`[../raw/<subdir>/<file>]`). Unidirectional: markdown → raw, never edit raw.
7. **rmdir empty source folders**, including the Drive top-level domain folder if fully drained. Originals stay in Drive through the `raw/` link.

## Per-domain README

Each `$OV/<domain>/README.md` documents:

- Tier (typically L2 working).
- Drive mapping (which top-level Drive folder feeds this domain).
- Subdirectory layout (`raw/`, plus structured markdown subdirs).
- Cross-domain references (e.g., housing references health for atopic-march cluster).
- Non-principles (e.g., "not a wiki tier").

Use `<paths.health>/README.md` and `<paths.housing>/README.md` as the templates.

## Naming hygiene in `$OV/<domain>/raw/`

- **Preserve original filenames** when possible. Original names retain provenance (timestamps, source-system IDs).
- **Preserve subfolder structure** when source is well-organized (e.g., `Housing/<complex>-<year_range>/` → `<paths.housing>/raw/<complex>-<year_range>/`).
- **Loose / orphaned files**: group into `_search/` or by-date subfolders inside raw to avoid clutter.
- **Format-conversion siblings**: name with same stem, different extension. Example: `<YYYY-MM-DD>-<clinic>-<procedure>.HEIC` (original) + `<YYYY-MM-DD>-<clinic>-<procedure>.jpg` (transcode). Markdown links to the readable one.

## Privacy boundaries

- `$OV/` is gitignored. Personal data (landlord names, MRNs, addresses, lease amounts, IDs) lives in `$OV` safely. The committed repo never sees it.
- Protocols / committed files (this file included) describe the **structure** generically. No personal names, addresses, employer names, or preference policy. `scripts/privacy_check.py` checks discovered private titles, gitignored exact terms, and staged blobs during `/lint` and `/push`; semantic review remains responsible for contextual disclosure.
- Encrypted vaults (1Password, etc.) are **out of scope**: never extract credentials from there into `$OV` plain text.
- Identity documents go to `raw/`, and notes with ID, bank, or salary details to a `secure/` folder; never through a Reflect capture, which pushes within seconds.
- Non-self entities (companion's medical, foster pets, etc.) get their own subdirectory under the domain (e.g., `<paths.health>/pet/`). Same protocol applies internally; cross-links from main self-line note "see also" but don't merge data.

## Audit / recovery

- After mv, verify file count at destination matches source pre-mv. `find "$OV"/<domain>/raw -type f | wc -l`.
- Annual or per-domain pass: spot-check that raw files referenced from structured markdown are reachable.
- Drive Trash retains rm'd files for 30 days. rmdir of an empty folder also goes to Trash. Recovery is from Drive web UI, not CLI.
- Never `rm -rf` for cleanup. Use targeted `rm <file>` and `rmdir <empty-dir>` so accidents are obvious.

## Post-ingestion verification

After a Drive → `$OV` ingestion sweep (especially a multi-domain pass), run the audit to catch gaps the protocol describes but a human eye misses:

```
uv run scripts/zk_audit.py            # human-readable report
uv run scripts/zk_audit.py --json     # machine-readable; used by /lint Phase 0b
```

The audit walks `$OV/` and surfaces eight categories of finding (advisory; only `--fix-links` mutates, by creating missing links):

| # | Category | What it flags | Action |
|---|---|---|---|
| 1 | Missing READMEs | Working-tier domains with no `README.md`. Protocol mandates one per domain (see "Per-domain README" above). | Write the README using `<paths.health>/README.md` or `<paths.housing>/README.md` as a template. |
| 2 | Raw without digest | `<domain>/raw/<sub>/` clusters where no `.md` in the working tier mentions `<sub>` by name. Substring heuristic; false negatives possible if a digest references its source by a synonym. | Either write a digest, or accept low-value status and `mv` the cluster to `<paths.archive>/<category>/`. |
| 3 | Archive ↔ working-tier overlap | Archive subtrees whose normalized name matches a working-tier domain (e.g., `archive/practical/health-admin` ↔ `health/`). Often pre-protocol residue duplicating active tiers. | Per-subtree decision: keep as historical archive, merge into the active working tier, or rename to disambiguate. Audit surfaces; user decides. |
| 4 | Root orphans + empty `.md` | `.md` files at `$OV/` root other than `README.md`; 0-byte `.md` files in working tiers. Empty `.md` under `archive/` aggregated as a count (pre-ingestion stubs, not new debt). | Move root orphans into a tier dir; delete or fill empty stubs. |
| 5 | Suspicious top-level dirs | Finder-duplicate names (` 2`, ` (2)`), empty dirs, skeleton dirs (no README, fewer than 3 entries). | Rename, remove, or build out. |
| 6 | Vault layout | `$OV` missing, not a Git work tree, or inside a file-sync folder; raw store unmounted; a `raw/`, `secure/`, or root `cache` that is a real folder, a misdirected link, or a store folder with no link. | Fix `$OV` or mount the store; `--fix-links` adds missing links; move real folders into the store by hand. |
| 7 | Duplicate Reflect titles | Reflect-visible notes whose title falls back to a filename another note shares (Reflect reads frontmatter `title:`, then the first H1, then the filename), leaving `[[Title]]` ambiguous. Groups whose other copies are only under `archive/` aggregated as a count. | Folders serve harness retrieval, not Reflect titles: add a flat, unambiguous frontmatter `title: "<Scope> <Kind>"`, or merge the duplicate notes. |

The audit is integrated into `/lint` as Phase 0b (advisory; never blocks). `/lint` surfaces a one-line summary per non-empty category; the full listings are read on demand via the script.

Findings are *advisory*; only missing links are auto-fixable. The audit reports gaps; consolidation, README authoring, and digest writing are user-driven follow-up work (typically a per-domain pass).

## Out of scope

- Live, actively-edited Google Docs in top-level Drive folders. Leave in place; ingest only when stable.
- Active financial / banking statements that may need original-format access for tax filing or audit. Copy (not move) into `$OV`; original stays in source until fiscal year closed.
- Files belonging to other people (collaborators, family). Even if dropped in your Drive, route by ownership: their material → their channel.

## Cross-references

- [[local-first-architecture.md]] — L1-L5 tier model, where this protocol slots in (raw landing → L1, structured `$OV` markdown → L2).
- [[raw-indexing.md]] — downstream pattern: cross-cutting clickable indexes over `$OV/<domain>/raw/`, the navigational layer for ingested archives.
- [[epistemic-hygiene.md]] — validation-depth taxonomy applies after ingestion.
- `<paths.housing>/README.md`, `<paths.health>/README.md` — current implementations of this protocol.
- `scripts/zk_audit.py` — post-ingestion hygiene audit (see "Post-ingestion verification" above).
