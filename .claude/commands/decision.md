---
description: Lightweight decision journal with analysis proportional to the stakes.
---
# Decision Journal

> Also reachable via `/hi <natural language>` (e.g., `/hi should I take the offer`,
> `/hi help me decide`, `/hi torn between`). See `harness/intents.toml`
> `[intents.decision]` for the full pattern list. Both paths execute this same procedure.

Capture a choice, the reason behind it, and what should cause it to be revisited.

## Trigger

User says something like:
- "I need to decide..."
- "Should I..."
- "Help me think through..."
- "I'm torn between..."

## Prerequisites

1. Reuse the current `decision` context projection from `$hi`; for direct
   invocation, run `uv run scripts/context_bundle.py --intent decision
   --format json`.

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
- When using both legs, follow `protocols/voice-dispatch.md` and
  `protocols/shadow-log.md`: start with
  `python3 scripts/shadow.py group-start --task decision --agent thinker`,
  resolve the native identity with
  `python3 scripts/shadow.py native-model --agent thinker`, close the group,
  and surface disagreement. A missing direct leg is a soft downgrade.

### Step 4: Decision Record

Don't push for a decision. If the user is ready, capture it. If not, capture the analysis.

## Output

**File:** `<paths.gtd>/decisions/<slugified-topic>.md`

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

After writing the decision file, emit a session log:
1. `Bash: uv run scripts/session_log.py --type decision --duration <minutes>`
2. `Edit` the created file to populate sections from session data (agents dispatched, searches, questions, frameworks, anomalies). The canonical fill-in guide lives in `protocols/session-log.md` § "Section Guidance". Leave empty sections with headers only. If the write fails, warn and continue.

## Wrap Up

The stable decision file in `<paths.gtd>/decisions/` is the durable output;
the dated session log is the process record. Daily notes are user-authored;
the system reads them but does not modify them. Tell the user whether the
decision record was created or updated and where to find it.
