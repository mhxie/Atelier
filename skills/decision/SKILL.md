---
name: decision
description: Lightweight decision journal with analysis proportional to the stakes.
---
# Decision Journal

> Also reachable via `/hi <natural language>` (e.g., `/hi should I take the offer`,
> `/hi help me decide`, `/hi torn between`). See `harness/intents.toml`
> `[intents.decision]` for the row's example phrases. Both paths execute this same procedure.

Capture a choice, the reason behind it, and what should cause it to be revisited.

## Trigger

User says something like:
- "I need to decide..."
- "Should I..."
- "Help me think through..."
- "I'm torn between..."

## Prerequisites

1. Reuse the current `decision` Repomix context artifact from `$hi`; for direct
   invocation, run `uv run scripts/context_bundle.py --intent decision`.

## The Decision Process

### Step 1: Frame the Decision

Establish the decision, real options, deadline, and binding constraints. Ask only
for information that could change the recommendation.

### Step 2: Search for Relevant History

Check `<paths.gtd>/decisions/` first. Update an existing topic record rather
than creating a sibling. Use semantic or exact search only when prior goals,
constraints, or decisions could change the answer.

### Step 3: Analyze Proportionally

- Clear or reversible choice: reason directly.
- If a framework would expose a blind spot, select one from
  `frameworks/cross-validation.md`. Add a second framework or a decision matrix
  only when the first pass leaves material uncertainty.
- For a costly, irreversible, or high-uncertainty choice, dispatch the native
  Thinker. Add the direct Thinker leg only when independent framing could
  materially change the outcome.
- When using both legs, follow `protocols/agent-handoff.md` → Responsibilities and Voice Legs, surface
  disagreement, and treat a missing direct leg as a soft downgrade.

### Step 4: Decision Record

Don't push for a decision. If the user is ready, capture it. If not, capture the analysis.

## Output

**File:** `<paths.gtd>/decisions/<slugified-topic>.md`

Present the proposed record or update and obtain approval before saving it.
The decision itself, agreement with the analysis, and permission to write are
distinct; do not treat a discussion as approval to persist it.

Slugify the topic for the stable filename: lowercase, replace spaces with
hyphens, and remove special characters (e.g., "SF vs NYC job" →
`sf-vs-nyc-job`). Dates belong in frontmatter and the decision log, never in
the filename. On later sessions, update the current decision and append a dated
log entry without rewriting prior entries.

```markdown
---
type: decision
status: open | decided | superseded
created: YYYY-MM-DD
updated: YYYY-MM-DD
review: YYYY-MM-DD or trigger
---

## Topic

[Decision description]

## Current Decision

[Current decision and rationale, or what remains open]

## Options Considered
1. [Option A]: [description]
2. [Option B]: [description]

## Evidence and Constraints
- [Fact, assumption, constraint, or unresolved uncertainty]

## Linked Notes
- [[Note Title]] — [relevance]

## Revisit Triggers
[Date, event, or evidence that should reopen the decision]

## Decision Log

### YYYY-MM-DD
- [Decision, change, or evidence update]
```

## Session Log

Run `uv run scripts/session_log.py --type decision --duration <minutes>` and fill
the compact Continuity, Anomalies, and Operations sections per
`protocols/session-log.md`. Keep the revisit trigger and actual write outcome;
do not duplicate the decision analysis in the log. Warn on a logging failure
without blocking the conversation.

## Wrap Up

The approved stable decision file is the durable output; the compact log
carries continuity. Daily notes remain read-only. Report whether the decision
record was created, updated, or not saved, and its location when saved.
