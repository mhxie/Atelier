---
description: Promote L2 working notes into a schema-compliant L4 wiki entry.
---
# /promote — Create L4 wiki entry from L2 source notes

> Also reachable via `/hi <natural language>` (e.g., `/hi promote to wiki`, `/hi canonize this`,
> `/hi create wiki entry`). See `harness/intents.toml` `[intents.promote]` for the row's
> example phrases. Both paths execute this same procedure.

Two-step workflow to promote existing L2 working notes (daily notes, reflections, agent findings, drafts) into a schema-compliant L4 wiki entry under `<paths.wiki>/`. Inspired by llm_wiki's analyze-then-generate ingest pipeline, adapted for atelier's claim-level trust architecture.

**Scope:** one wiki entry per invocation. The user names a topic or set of source notes; the command produces a draft wiki entry with pre-populated `@anchor` markers for user review.

## Prerequisites

1. Source notes must already exist under `$OV/` (any tier except L4).
2. `protocols/wiki-schema.md` defines the target format.
3. `scripts/trust.py` validates structural integrity after creation.

## Arguments

The user provides one of:
- A **topic** (e.g., "promote distributed locking") and the Researcher finds relevant source notes.
- One or more **file paths** (e.g., `<paths.daily_notes>/2026-04-08.md`, `<paths.agent_findings>/lance-brief.md`).

## Process

### Phase 1: Gather and Analyze (Researcher)

Dispatch the **Researcher** agent:

```
Research brief request: identify all notes related to [topic/paths] that contain
claims suitable for L4 promotion. For each candidate:
  - Extract factual claims (not opinions or speculation)
  - Note any external sources referenced (URLs, paper citations, DOIs)
  - Flag claims that overlap with existing wiki entries under <paths.wiki>/
  - Report the validation depth of each source note (L1 raw, L2 working, L3 external)

Search strategy:
  1. scripts/semantic.py query "[topic]" --top 20  (primary)
  2. Grep for exact terms across $OV/ (structural follow-up)
  3. Read candidate files in full
  4. Cross-reference against existing <paths.wiki>/ entries via trust.py --json

Return a structured brief with:
  - candidate_notes: [{path, title, relevant_claims: [text, source_url?]}]
  - existing_wiki_overlap: [{wiki_entry, overlapping_claims}]
  - suggested_title: proposed H1 for the new wiki entry
  - suggested_claims: [{claim_text, anchor_candidates: [{type, id, url}]}]
```

Present the Researcher's brief to the user. Ask:
1. Confirm or adjust the suggested title
2. Confirm or prune the claim list
3. Flag any claims that should NOT be promoted (too speculative, personal opinion, etc.)

### Phase 2: Generate Draft (Curator)

After user approval of the brief, dispatch the **Curator** agent with the approved claims:

```
Wiki entry draft request.

Target path: <paths.wiki>/<Title>.md   (title-case with spaces, matching the H1)
Schema reference: protocols/wiki-schema.md

Approved claims and anchors:
[paste the user-approved claim list from Phase 1]

Instructions:
  1. Generate a wiki entry following protocols/wiki-schema.md exactly:
     - H1 title
     - ## Summary (prose synthesis, no anchors)
     - ## Claims with [C1], [C2], ... headings
     - Each claim gets a body paragraph and ^cn block ID
     - Each claim gets a fenced ```anchors block with @anchor markers
     - ## Revision Log with today's date and creation context
  2. For @anchor markers:
     - Use the appropriate type (s2, arxiv, doi, isbn, url, gist)
     - Set valid_at to today's date
     - Include the source URL or identifier from the Researcher's brief
  3. Do NOT add @cite markers yet (those require target entries to exist).
     Leave a comment in the Revision Log noting which existing wiki entries
     could be cross-referenced once the entry is created.
  4. Do NOT add @pass markers (those come from post-creation review).
  5. Return the full markdown content as a draft.
```

### Phase 3: Validate and Write

1. **Present the draft** to the user for review. Show the full markdown.
2. On user approval, **write the file** to `<paths.wiki>/<Title>.md` (title-case with spaces, matching the H1).
3. Run `scripts/trust.py --note "<paths.wiki>/<Title>.md"` to verify structural integrity.
   - If errors: show them, ask the user if they want to fix or abort.
   - If clean: report the initial trust score (will be raw PageRank, no floor until a reviewer pass).
4. Run `scripts/lint.py` to check for corpus-level issues (shared anchors, etc.).
5. Regenerate the wiki index: `scripts/trust.py --index`.

### Phase 4: Localized Shadows

After the English entry passes validation, generate a shadow copy for each language configured under `[paths.wiki_localized]` in `harness/paths.local.toml`. If the table is empty, skip this phase.

For each `(language_code, shadow_dir)` entry:

1. **Translate** the entry to the target language following these rules:
   - Translate all prose (Summary, claim text, body paragraphs, Revision Log)
   - DO NOT translate: technical terms (e.g., Lance, Ray Data, PyArrow, MVCC), code identifiers, URLs, file paths
   - DO NOT translate `@anchor`/`@pass` blocks or `@cite` lines: copy exactly as-is
   - DO NOT translate block IDs
   - Keep the `# Title` in English (filename must match the source)
   - Add a localized backreference at the top, e.g. for Chinese:
     `> 本文为 [[English Title]] 的中文版本。核心技术术语保留英文原文。`
2. **Write** to `$OV/<shadow_dir>/<Title>.md` (same filename as the English version).
3. This step is automatic and does not require additional user approval.

### Phase 5: Post-Creation Suggestions

After successful creation, suggest next steps:

1. **Add @cite edges:** If the lint report shows `shared-anchor-no-cite` findings involving the new entry, suggest specific @cite markers to add.
2. **Request reviewer pass:** "Run a review session to add `@pass: reviewer | status: verified` markers, which unlocks the 0.1 trust floor."
3. **Update the wiki index:** Already done in Phase 3 step 5.
4. **Snapshot anchors to Readwise:** Save all `url:` anchors to Readwise with `anchor-evidence` + category tag, backfill `readwise:` document IDs.

## Example Invocation

```
User: /promote distributed locking patterns
```

The command finds notes about distributed locking in daily notes, agent findings, and reading notes. Extracts claims about lock granularity, consensus protocols, lease-based mechanisms. Produces a wiki entry like:

```markdown
# Distributed Locking Patterns

## Summary
[synthesis of the key patterns...]

## Claims

### [C1] Lease-based locks with TTL are preferred over indefinite locks in distributed systems

[body with evidence...] ^c1

```anchors
@anchor: url:https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html | valid_at: 2026-04-11
```

## Revision Log

- 2026-04-11: Initial draft promoted from [[2026-04-08]] daily note and [[Deep Dive Brief]] agent finding. Candidate @cite edges: [[Related Wiki Entry A]] (shared concept overlap), [[Related Wiki Entry B]] (shared mechanism overlap).
```

## Error Handling

- **No relevant source notes found:** "No notes found related to [topic]. Try a different search term, or provide specific file paths."
- **All claims already covered by existing wiki entries:** "The claims in these notes are already covered by [existing entry]. Consider adding @cite markers to the existing entry instead of creating a new one."
- **Structural integrity failure after write:** Show the specific errors from trust.py. Offer to fix in place or delete and retry.
- **Curator unavailable:** The orchestrator can generate the draft directly using the schema as a template, though the Curator's Content Preservation Checklist adds safety.
