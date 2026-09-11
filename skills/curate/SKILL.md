---
name: curate
description: Goal-aware content curation and inbox triage with attributable reading episodes.
---
# Curate

> Also reachable via `/hi <natural language>` (e.g., `/hi triage inbox`, `/hi curate readwise`,
> `/hi score my inbox`). See `harness/intents.toml` `[intents.curate]` for the row's
> example phrases. Both paths execute this same procedure.

Goal-aware content curation. Pulls from content sources, scores against your active goals and directions, and routes items into tiers.

## Flow

### 1. Load Context (orchestrator)

Reuse the current `curate` context artifact from `$hi`; for direct
invocation, run:

```bash
uv run scripts/context_bundle.py --intent curate
```

Use only the packed route-selected profile files as goal and identity context.

Read `protocols/decision-ledger.md` → Reading feedback loop. Capture the
selection policy and routed context with `decisions.py reading-policy`,
using the actual model that will perform triage. The helper stores replay
inputs privately once; retain its compact policy ID.
Read bounded explicit preferences with `decisions.py reading-evidence`; use
their reasons to refine selection, retaining uncertainty and context. Include
that evidence projection in the policy snapshot. Keep one episode ID for this
run and preserve each item's source ID through the reading handoff.

### 2. Dispatch Triage Agent (ad-hoc)

Dispatch a **general-purpose agent** (not a named agent) with this prompt structure:

```
You are triaging a content inbox against the user's active goals and directions.

## Goals and Directions
[paste relevant sections from profile/directions.md — current era, near-term goals, learning directions]

## Attributable Preferences
[paste the bounded reading-evidence projection, including reasons, uncertainty,
and proposed_action; approvals of archive actions are not positive taste]
Apply this feedback to selection when its context matches the present task.

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
   Also return the complete structured candidate list as a local JSON artifact
   for the orchestrator: source ID, title, category, bounded source summary,
   canonical URL, author, reading_time, word_count, tags, proposed action, and
   reason. Preserve available selection inputs, omitting unavailable fields
   rather than fabricating them. This is proposal evidence.
```

### 3. Present Results (orchestrator)

Batch candidates as `proposed` with the captured policy ID using the typed
reading-event helper. Preserve stable event IDs for retries. After presentation,
batch `shown` events for the
items actually displayed. Internal archive candidates are not shown merely
because the agent wrote them to a file.

Show the user:
- Stats (inbox size, tier breakdown)
- Deep Read candidates with goal-relevance reasons
- Ask: "Want me to tag these in Readwise and archive the rest?"

### 4. Execute (orchestrator)

On approval:
- Record `approved` for the authorized operation on each identified item;
  record any explicit per-item rejection/defer and its actual reason. Batch
  approval does not establish that the user consumed or liked the content.
- Tag deep-read items: `readwise reader-add-tags-to-document --document-id <id> --tag-names deep-read`
- Tag digest items: `readwise reader-add-tags-to-document --document-id <id> --tag-names digest`
- Archive skipped items: `readwise reader-move-documents --document-ids <id1>,<id2>,<id3> --location archive`
- Tag all processed items: `readwise reader-add-tags-to-document --document-id <id> --tag-names auto-triaged`

### 5. Bridge to Reading (optional)

If the user wants to read something now, transition to `/hi → Read` with the selected item.
Carry its episode/item/policy IDs into Read; subsequent feedback keeps the
policy identity from selection time.

## Integration Points

- **`profile/reader_persona.md`** — if it exists, the triage agent can reference it for taste calibration alongside goals

## Adding New Sources

To add a new content source (e.g., Zhihu, Twitter):
1. Create `sources/<name>.md` with setup, CLI commands, and output format
2. Update the triage agent prompt to also pull from the new source
3. The same tier logic applies — deep-read / digest / archive
