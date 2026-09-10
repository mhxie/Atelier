---
description: Read and discuss a source with one reading worker; expand perspectives, evidence gathering, or review when the task needs it.
---
# Read & Discuss

Procedure for Read intent. Owns Reader/Scholar selection, Readwise prefetch, source backup, and the three read modes (Read & Discuss, Focused Read, Multi-Lens Read).

## Menu

| Option | Label | Description |
|--------|-------|-------------|
| 1 | **Read & Discuss** | Quick read + interactive discussion (default) |
| 2 | **Focused Read** | Pick 1-2 specific lenses to focus on |
| 3 | **Multi-Lens Read** | Examine the text through all four lenses |

## Reader vs Scholar selection (applies to all three Read modes)

Start with one **Reader**, or one **Scholar** when `word_count > 8000`, the
source is under `<paths.papers>/` or `<paths.preprints>/`, or frontmatter
declares `difficulty: hard`. Both use the same reading workflow; their voice
bindings differ. This procedure owns the selection rule.

## Local cache check (before fetching; applies to all three Read modes when the source is a paper or external URL)

If the source is a paper or an external URL (arXiv, conference PDF, a paper named by title), check local material FIRST and only hit the web on a miss. The cached copy is often already on disk; a web round-trip before checking it is wasted latency.

1. Surface prior local material. Run `uv run scripts/semantic.py query "<title
   or distinctive keywords>" --top 5 --format json`. The central
   corpus policy returns current authored notes and may return compact
   locators for assets under a `raw/` cluster. It does not extract a binary PDF
   or index `<paths.cache>/`, so treat these hits as related context or a raw
   location hint, not as proof that the paper PDF is cached.

2. Test for an already-cached PDF. Glob the flat paper store for a file matching the author or title: `ls "$OV"/papers/ "$OV"/preprints/ 2>/dev/null | grep -i "<firstauthor-or-distinctive-keyword>"` (resolve `papers` / `preprints` via `harness/paths.toml`). If a match exists, pass that local path to the reading agent and skip the web fetch entirely.

3. Fetch from the web only if both the note query (step 1) and the PDF glob (step 2) miss. After a web fetch, cache the PDF into `<paths.papers>/` so the next read is a local hit (naming convention in `sources/local-papers.md`).

4. If the resolved source is a local PDF, materialize or reuse the paper text cache before dispatch. Run `python3 scripts/paper_cache.py "<resolved-pdf-path>"`; it returns `<paths.cache>/<paper-slug>/`. Pass that directory as `cache_path` to every Reader or Scholar. For a normal URL or a non-PDF source, skip this cache step and use the source directly. Follow the shared scratch rule in `CLAUDE.md`; one-session page renders use `mktemp -d` and are removed when reading finishes.

## Prefetch Step (Readwise podcasts, videos, articles; applies to all three Read modes)

If the source is a Readwise podcast, video, or article (user provides a Readwise URL, `document_id`, or names a podcast), **cache the transcript once before dispatching any reading agent (Reader or Scholar)**. Independent fetches across parallel reading-agent instances are the failure mode this step exists to avoid (same reasoning as the paper cache).

1. Resolve `document_id`. If the user gave a title, find it: `readwise reader-search-documents --query "<keywords>"` → pick the match.
2. Snapshot content: `readwise reader-get-document-details --document-id <id> | jq -r '.content' > <paths.cache>/rw-<id>.md`
3. Pass `cache_path: <paths.cache>/rw-<id>.md` to every reading-agent dispatch (Reader or Scholar). The convention is documented in `.claude/agents/reader.md` § "Readwise transcript cache"; Scholar follows the same convention.
4. For podcasts specifically: also pass the guest name (parsed from title) and host name (from the `author` field) in the dispatch prompt so the reading agent doesn't have to re-infer for citation.

## Backup to Readwise (optional final step in every Read mode)

After the reflection file is saved, make one `readwise reader-create-document`
call only if the user explicitly authorized backing up this source to Readwise.
Approval to save the local reflection does not authorize this external write.
Without backup authority, skip it without blocking the local save. The reflection
in `<paths.reflections>/` remains the durable artifact.

**Skip conditions (do NOT call the CLI):**
- The user has not explicitly authorized this Readwise backup.
- Input was a Readwise URL or `document_id` (already in Readwise; the Prefetch Step handled it).
- Input was a local `[[Note Title]]` (no source URL exists).
- Input was a transcript paste with no accompanying URL (nothing to back up).
- `readwise` CLI is not installed in the environment (`command -v readwise` returns nothing). Backup is best-effort; absence of the CLI is not a session error.

**When it runs (orchestrator, not a separate agent):**

```bash
readwise reader-create-document \
  --url "<canonical-source-url>" \
  --tags "<comma-list>" \
  --category "<article|pdf|video|podcast>"
```

- `--url`: canonical source URL. For arXiv use the abs page (`/abs/<id>`), not the PDF URL.
- `--tags`: 3-5 topic tags selected by the orchestrator within the authorized backup.
- `--category`: default `article`; use `pdf` for arXiv/PDF papers, `video` or `podcast` for transcripts.

Print the resulting Readwise URL or `document_id` to the user as a one-liner confirmation. Do not pre-check for duplicates: if Readwise re-creates a doc, the second `document_id` is fine. Do not loop on errors; if the call fails (network, auth), report the error and continue, since the reflection file is already saved.

## Initial-analysis persistence checkpoint

For a source handed off from Curate, retain its reading episode/item/policy
IDs. Follow `protocols/decision-ledger.md` → Reading feedback loop for any
explicit reading decision, consumption observation, or usefulness feedback
that arrives during discussion. Record the user's reason and source turn;
do not ask for a rating merely to complete telemetry. An analysis completing
or a reflection being saved does not prove the user consumed or valued it.
For direct reading, look up `decisions.py reading-episodes --item <source-id>`
when attribution is needed. Use a prior episode only when its origin is
unambiguous; a truncated or multiple-match result does not establish that.
If no unambiguous curation episode exists,
use a new direct-reading episode with `policy_id: direct`; its feedback may
inform taste but receives no curation-policy outcome credit.

Immediately after the first complete reading analysis returns, run the reading
checkpoint defined in protocols/session-log.md before presenting the analysis
or entering discussion. Do not wait for a future write-back request. Resolve a
collision-free reading session-log path, then create the complete log and its
filled Reading Capsule in one Write/Edit operation. Do not use
`scripts/session_log.py` for reading because skeleton-then-fill leaves an
interruption window.

## Per-option flows

Use the supplied source; ask only when the source or a requested focus is
missing. Apply the matching cache/prefetch branch before dispatch.

- **Read & Discuss:** One Reader or Scholar examines the argument and its
  evidence, starting with the Critical lens. Complete the Initial-analysis
  persistence checkpoint, present the analysis, and discuss it with the user.
- **Focused Read:** The same worker applies the requested lens or lenses,
  keeping their findings distinct. Complete the same checkpoint before
  presentation or discussion.
- **Multi-Lens Read:** On request, follow the Reading Hub flow below.

Add a specialist only for a concrete task the reading has exposed: **Researcher**
for a needed connection to local notes, one **Scout** for bounded verification,
**Thinker** for a consequential framework question, or **Challenger** for a specific
assumption that needs independent scrutiny. Name that task in the dispatch.
Use **Synthesizer** only when combining substantial independent briefs needs
separate synthesis. Additional agents are not a completion requirement.

## Reading Hub Flow (Multi-Lens Read)

1. One Reader or Scholar applies Critical, Structural, Practical, and
   Dialectical perspectives in separate sections. Use independent readers
   when the user requests independent takes or a specific disputed claim
   warrants them; parallelize only those independent tasks.
2. The orchestrator presents convergence, disagreement, and remaining
   uncertainty across the findings. Complete the Initial-analysis persistence
   checkpoint before presenting the report in Chinese or entering discussion.
3. Follow the user's questions. Expand a lens or seek specific evidence when
   needed; there is no fixed fanout or mandatory synthesis stage.

## Source verification and reflection writeback

Every mode checks quotations and attributed claims against the source and
distinguishes the author's claims from the reader's analysis. Verify external
claims with appropriate sources or leave them explicitly unverified. If the
source cannot be accessed, state the limitation instead of reconstructing it.

Reading review: add one independent review for substantive, uncertain, or
consequential claims, a significant disputed interpretation, or when the user asks. Dispatch
the targeted **Reviewer** in Session Review mode and resolve findings before writeback.
An ordinary reading reflection does not require a Reviewer/Challenger pair.

Present the proposed reflection and obtain explicit user approval before
writing `<paths.reflections>/YYYY-MM-DD-reading-<slug>.md`. Discussion alone
does not require a reflection file. Never write reading conclusions to daily
notes. Include full source text only when supplied or locally retained with
redistribution rights; otherwise preserve a source locator and bounded
excerpts. After an approved reflection is saved, apply Backup to Readwise's
separate authority and skip conditions above.
