# Local-First Architecture

The knowledge layer is plain-text Markdown under the user's `$OV` vault root.
Agents read and write it directly; there is no remote note-store mirror.
Deterministic file access supports the wiki schema, trust graph, and lint.

## The Layers

L1–L5 describes storage and certification depth, not human/AI provenance.
The orthogonal alloy / wiki entry / `#solo-flight` validation taxonomy lives
in `protocols/epistemic-hygiene.md`.

| Layer | Contents and location | Contract |
|---|---|---|
| L1: Raw capture | Readwise inbox, `<paths.inbox>/`, `<domain>/raw/`, `<paths.cache>/` | Unstructured input. Raw source artifacts are durable; cache is disposable. Readwise is cloud-only, queried explicitly through its connector or CLI. |
| L2: Working | `<paths.daily_notes>/`, `<paths.reflections>/`, `<paths.research>/`, `<paths.agent_findings>/`, `<paths.wip>/`, `<paths.gtd>/`, Reflect-created `<paths.notes>/`, domain notes | Most active thinking: searchable and citable, alloy by default, not certified. Inactive topic notes stay in `<paths.archive>/`. |
| L3: External receipts | `<paths.papers>/`, `<paths.preprints>/`, curated reading artifacts | Papers, local PDFs, and externally anchored structured paper reviews. Preprints belong here, not among L2 free-writes. Receipts support L4 `@anchor` markers. |
| L4: Locally certified | `<paths.wiki>/` | Location, not `#wiki` or `#compiled-truth`, identifies wiki entries. Only this subtree participates in trust propagation, bi-temporal anchoring, and wiki structural lint. |
| L5: Foundation | Reserved for settled, textbook-level knowledge | No folder or active workflow yet. |

`protocols/wiki-schema.md` owns L4 claims, anchors, `@cite` graph edges, and
validation. `scripts/trust.py` walks only the wiki subtree and reports per-note
scores; everything outside stays working knowledge or receipts. Paper IDs use
`s2:`, `arxiv:`, or `doi:`; articles use `url:` or a Readwise document ID.
`sources/local-papers.md` owns paper retrieval.

Promotion is opportunistic and upward: capture becomes working material,
anchored recurring claims earn wiki entries, and curated receipts enter L3.
Invalidation is additive through bi-temporal markers, not destructive demotion.

## Project Layout

The version-controlled Atelier repo owns public harness code, commands, roles,
protocols, frameworks, and source-handling instructions. Personal content lives
in `$OV`; private harness configuration is gitignored. `harness/paths.toml`
and its local override own the path inventory, not a second directory tree here.
Repo paths are project-relative; `<paths.*>` resolves through that registry.

`<paths.sessions>/` holds process records.

## Search Projections

The physical vault is authoritative; the machine-local QMD index is a projection.
`active` is the default authored-knowledge scope. `raw`, `archive`, `inbox`,
and `process` are explicit deeper scopes; `all` is their audit/recall union,
not the default. `process` maps to `<paths.sessions>/`; raw search covers
readable text, not binary locators. Readwise remains external and opt-in.

Operational directories such as nested `cache/`, `_meta/`, `_routine_prompts/`,
`.trash/`, and `_tools/` never enter semantic retrieval. `scripts/semantic.py`
owns collections and exclusions; QMD owns synchronization, chunking, embeddings,
and retrieval. Custom score adjustments are retired. `sources/semantic.md`
owns the CLI, exact scope boundaries, setup, and hardware profiles.

Reflect maintains its own index in `$OV/.reflect/index.sqlite` while the desktop
app runs. Agents reach it only through the bundled `reflect` CLI (see AGENTS.md).

## Source of Truth

`$OV` is canonical for L2–L4 content durably written and confirmed on disk.
Readwise is the L1 source for its cloud inbox. In-flight routine output is
provisional evidence in its hosting session, not proof of a persisted artifact;
`protocols/remote-routines.md` owns artifact attestation and delivery channels.

Tier authority does not settle fact authority. When one fact appears in more
than one L2 file, exactly one file owns its state; the others cite that owner
and do not restate it. A task tracker may keep a completion checkbox as a
scheduling record, but that checkbox is derived: it carries a link to the
owning file, the owning file is updated first, and the owning file wins on
conflict. A consumer that finds a disagreement reports it as a conflict rather
than trusting whichever copy it read first. Staleness markers such as
`freshness: required` catch an old view, not two views that contradict.

The host filesystem handles device sync and backup. `$OV` may also be a private
Git repo; `protocols/repo-conventions.md` owns its layout. There is no two-way
note-store sync or mirror ledger. The system does not auto-commit or auto-push
the vault; user-driven Git retains its own authority.

Daily notes are user-authored and read-only to the system. Curator refuses
daily-note targets; only Scribe `daily_note` may record user-dictated text
verbatim, never orchestrator transcription. Other tiers use Curator proposals
and orchestrator writes after approval. `AGENTS.md` owns the shared write
boundaries and declared capture/session exceptions.

## Migration Strategy: Opportunistic, Not Big-Bang

New wiki entries use Curator drafts and approved orchestrator writes. Existing
L1/L2 or archived notes are structured into L4 only when their claims are about
to anchor a new wiki claim; preserve the original capture untouched. There is
no bulk migration or goal to hoist the vault. Most thinking remains L1/L2.

## Aggregation vs. Detail (orthogonal to L1-L5)

Within a tier, detail files own subject facts; aggregate trackers are manually
mirrored views and may lag. Before quoting an aggregate as authoritative, run
`scripts/aggregate_freshness.py --discover --stale-only` and cross-check any
flagged file against its subject source. This is a read-time warning against
shadow state, not automatic write-time propagation.

Date-based freshness checks opt in with leading YAML frontmatter:

```yaml
---
subjects: <path-to-subjects-dir>  # relative to $OV or absolute
freshness: required
---
```

Discovery ignores unmarked files and groups marked files by `subjects`.
The script owns excluded directories and date resolution (leading update
marker, YAML date, then mtime). `--stale-only` is silent when fresh; explicit
`--subjects` / `--aggregates` still supports ad-hoc and transitional checks.
Adoption is forward-looking. Unlinked prose and numerical aggregates remain manual.

## Derived task status

For ledger-backed tasks, put a stable `<a id="benefit-a"></a>` in the owning
Markdown table row. Its third column owns Status: `☐` or `📅` means open,
`✅` done, `🚫` cancelled. Add one `[sot](<../domain/ledger.md#benefit-a>)`
to its GTD checkbox. Paths are relative to GTD; owner files stay inside the
vault and outside GTD and daily notes. Split tasks with distinct owners.

Update the owner row and evidence first. `todos.py` derives linked status for
list, stale, digest, and daily brief; `check` reports saved-marker drift.
Run `sync --file <quarter>.md` to preview, then `--apply` under the existing
write authority. It validates references and source snapshots before replacing
one GTD file atomically, then reads it back. Reruns are idempotent; simultaneous
external edits after validation remain a filesystem race. Missing, duplicate,
or invalid owners fail visibly. Linked status always follows the owner,
including reopening. For a separate scheduling cancellation or migration,
change `[sot]` to `[source]` before marking `[~]`; the citation remains ordinary.

`check` covers explicit links only. Historical prose stays dated; current
summaries link to owners instead of copying state. Other executor/planner pairs
retain an `upstream: <path>#<anchor>` reference and are backfilled together.

## Per-Agent Contract

Role briefs in `agents/` own retrieval, proposals, and review, with
`protocols/agent-handoff.md` owning responsibilities and dispatch gates.
After an approved wiki write, run `scripts/trust.py --note <path>` for structural
verification and initial scores. Only Reviewer signoff warrants a
`@pass: reviewer | status: verified` marker; `skills/lint/SKILL.md` owns
corpus checks. Promoted briefs belong in `<paths.agent_findings>/`, not cache,
and follow the same orchestrator write boundary.
