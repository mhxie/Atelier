---
description: Open-ended exploration for forgotten connections and emerging themes.
---
# Open Exploration

> Also reachable via `/hi <natural language>` (e.g., `/hi explore`, `/hi what am I missing`,
> `/hi forgotten connections`). See `harness/intents.toml` `[intents.explore]` for the row's
> example phrases. Both paths execute this same procedure.

Free-form exploration session for when the user doesn't have a specific question — they just want to think. This is the "what's interesting?" command.

## Prerequisites

1. Reuse the current `explore` context projection from `$hi`; for direct
   invocation, run `uv run scripts/context_bundle.py --intent explore
   --format json`.
2. Treat its bounded reflection excerpts as the continuity seed. Read a
   complete reflection only if a selected thread requires it.

## The Exploration Process

### Step 1: Cast a Wide Net

Semantic search leads; lexical grep fails by design for exploration. Run 3-4 diverse searches over `$OV/`:

1. **Semantic search on a recent theme:** `Bash: uv run scripts/semantic.py query "<recent topic>" --top 10 --context --format json` (conceptual neighbors). If results are thin, reframe and retry.
2. **Tag exploration:** `Bash: grep -rohE '#[A-Za-z][A-Za-z0-9_-]*' "$OV"/ | sort | uniq -c | sort -rn | head -40` → pick a tag the user hasn't engaged with recently → `Grep(pattern: "#<tag>", path: "$OV/")`.
3. **Time-shifted search:** `Bash: find "$OV"/daily-notes "$OV"/reflections -type f -name "*.md" -newermt "<12 months ago>" ! -newermt "<6 months ago>"` → `Grep` for the current interest inside that set. What were they thinking about this 6-12 months ago?
4. **Cross-domain search:** `Grep` for a term from one life area in another. E.g., if career-focused recently, grep for "health" or "learning" inside `$OV/`.

### Step 2: Surface the Surprising

From search results, identify:
- **Forgotten threads:** Notes the user wrote but probably forgot about
- **Unexpected connections:** Two notes from different domains that relate
- **Evolution:** How their thinking on a topic has changed over time
- **Gaps:** Areas of life with no recent notes (signal of avoidance or completion)

### Step 3: Present as Provocations

Don't summarize — provoke. Present 3-4 "sparks":

> **Spark 1: The Forgotten Thread**
> "Six months ago you wrote about [topic] in [[Note Title]]: '[quote]'. That connects to what you're doing now with [current focus]. Have you revisited this?"

> **Spark 2: The Cross-Domain Link**
> "Your [[Career Note]] mentions [concept]. Interestingly, your [[Personal Note]] from last month uses almost the same language about [different topic]. Is there a pattern here?"

> **Spark 3: The Evolution**
> "In [[Old Note]], you believed [X]. In [[Recent Note]], you're leaning toward [Y]. What shifted?"

> **Spark 4: The Silence**
> "I notice you haven't written about [topic] since [date]. Is that intentional, or has it slipped?"

### Step 4: Follow the Thread

Let the user pick which spark interests them. Then:
- Pull more related notes via `uv run scripts/semantic.py query` (or `Grep` once the concept has a name)
- Apply a relevant framework if appropriate
- Ask deepening questions (use Challenger's question taxonomy)

## Output

**File:** `<paths.reflections>/YYYY-MM-DD-exploration.md`

```markdown
# Exploration — YYYY-MM-DD

## Sparks Surfaced
1. [Spark description] — [[Source Notes]]
2. [Spark description] — [[Source Notes]]
3. [Spark description] — [[Source Notes]]

## Thread Followed
[Which spark the user engaged with and what emerged]

## Connections Discovered
[New links between notes/ideas]

## Open Questions
[Questions that emerged but weren't resolved]

## Notes Referenced
[All [[Note Title]] links]

## Session Meta
- User engagement: high / medium / low
- Surprise factor: yes / no
```

## Session Log

After writing the exploration file, emit a session log:
1. `Bash: uv run scripts/session_log.py --type exploration --duration <minutes>`
2. `Edit` the created file to populate sections from session data (agents dispatched, searches, questions, frameworks, anomalies). The canonical fill-in guide lives in `protocols/session-log.md` § "Section Guidance". Leave empty sections with headers only. If the write fails, warn and continue.

## Wrap Up

The exploration file in `<paths.reflections>/` is the durable session output. Daily notes are user-authored; the system reads them but does not modify them.
