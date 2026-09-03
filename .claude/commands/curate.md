---
description: Goal-aware content curation and inbox triage.
---
# Curate

> Also reachable via `/hi <natural language>` (e.g., `/hi triage inbox`, `/hi curate readwise`,
> `/hi score my inbox`). See `harness/intents.toml` `[intents.curate]` for the row's
> example phrases. Both paths execute this same procedure.

Goal-aware content curation. Pulls from content sources, scores against your active goals and directions, and routes items into tiers.

## Flow

### 1. Load Context (orchestrator)

Reuse the current `curate` context projection from `$hi`; for direct
invocation, run:

```bash
uv run scripts/context_bundle.py --intent curate --format json
```

Use only the projected profile excerpts as goal and identity context. Retrieve
a named profile section deliberately if the projection marks it truncated and
that section is load-bearing for a score.

### 2. Dispatch Triage Agent (ad-hoc)

Dispatch a **general-purpose agent** (not a named agent) with this prompt structure:

```
You are triaging a content inbox against the user's active goals and directions.

## Goals and Directions
[paste relevant sections from profile/directions.md — current era, near-term goals, learning directions]

## Task
1. Run BOTH commands in parallel (two Bash calls in one response):
   - `readwise reader-list-documents --location new --limit 30 --response-fields title,author,summary,category,word_count,reading_time,saved_at,tags,source_url --json`
   - `readwise reader-list-documents --location later --limit 20 --response-fields title,author,summary,category,word_count,reading_time,saved_at,tags,source_url --json`

   Note: `id` is NOT a valid `--response-fields` value (the API rejects it). The document `id` is returned implicitly on every result as the top-level `id` key.

2. For each item, assign a tier. **Thresholds are commitment-adjusted by `category`.** A 69-minute podcast and a 1,200-word article are not the same ask:

   | Category | deep-read means | digest | archive |
   |---|---|---|---|
   | `article`, `rss`, `email` | directly relevant to an active goal; worth ~10–30 min close read | interesting context, summary captures it | low relevance |
   | `podcast`, `video` | relevant to an active goal AND `reading_time` ≤ ~90 min (the user will actually listen) | worth skimming highlights/summary only | too long or not relevant |
   | `book`, `pdf`, `epub` | rarely auto-promoted; only if user explicitly tagged `#deep-read` | save summary, chapter-scan worthy | low relevance |
   | `tweet` | almost never deep-read | quote worth keeping | archive |

3. Podcast metadata: the Readwise `author` field is the show **host**, not the guest. Parse the title for the guest. Common patterns:
   - `"20VC: <Guest> on <Topic>"`
   - `"<Show> with <Guest>: <Topic>"`
   - `"Episode N: <Guest>, <Topic>"`

   Cite the guest in the relevance reason, not the host (the host is the same every episode). Flag any auto-transcript as potentially misrendering proper nouns; do not write a guest name to the triage file without high confidence.

4. Write the ranked list to `<paths.cache>/triage-YYYY-MM-DD.md` with format:

   ```markdown
   # Triage: YYYY-MM-DD

   ## Deep Read
   - `id:DOC_ID` [Title](https://read.readwise.io/read/DOC_ID) (<category>, <commitment: e.g. "1h 9m listen" for podcasts or "~12 min read" for articles>, by <author or guest>): *one-line reason tied to a specific goal*

   ## Digest
   - `id:DOC_ID` [Title](https://read.readwise.io/read/DOC_ID) (<category>, by <author or guest>): *one-line summary*

   ## Archive
   - `id:DOC_ID` [Title](https://read.readwise.io/read/DOC_ID): *why skipped*

   ## Stats
   - Inbox: N items | Later: N items
   - Deep read: N | Digest: N | Archive: N
   - Podcasts: N (total listen: Xh Ym) | Articles: N | Other: N
   ```

5. Return ONLY: the stats line + the Deep Read section (titles and reasons). Do not return the full list.
```

### 3. Present Results (orchestrator)

Show the user:
- Stats (inbox size, tier breakdown)
- Deep Read candidates with goal-relevance reasons
- Ask: "Want me to tag these in Readwise and archive the rest?"

### 4. Execute (orchestrator)

On approval:
- Tag deep-read items: `readwise reader-add-tags-to-document --document-id <id> --tag-names deep-read`
- Tag digest items: `readwise reader-add-tags-to-document --document-id <id> --tag-names digest`
- Archive skipped items: `readwise reader-move-documents --document-ids <id1>,<id2>,<id3> --location archive`
- Tag all processed items: `readwise reader-add-tags-to-document --document-id <id> --tag-names auto-triaged`

### 5. Bridge to Reading (optional)

If the user wants to read something now, transition to `/hi → Read` with the selected item.

## Integration Points

- **`/weekly`** — (planned) pulls `#digest` tagged items from Readwise as a "Reading Digest" section
- **`/hi → Read`** — (planned) surfaces `#deep-read` tagged items as reading suggestions
- **`profile/reader_persona.md`** — if it exists, the triage agent can reference it for taste calibration alongside goals

## Adding New Sources

To add a new content source (e.g., Zhihu, Twitter):
1. Create `sources/<name>.md` with setup, CLI commands, and output format
2. Update the triage agent prompt to also pull from the new source
3. The same tier logic applies — deep-read / digest / archive
