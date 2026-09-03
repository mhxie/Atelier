---
description: Assessment of physical, mental, emotional, and social energy patterns.
---
# Energy Audit

> Also reachable via `/hi <natural language>` (e.g., `/hi I'm drained`, `/hi energy audit`,
> `/hi burnt out`). See `harness/intents.toml` `[intents.energy-audit]` for the row's
> example phrases. Both paths execute this same procedure.

Deep assessment of where your energy goes — physical, mental, emotional, and social. Identifies energy drains and sources to optimize for sustainable high performance.

## Prerequisites

1. Reuse the current `energy-audit` context projection from `$hi`; for direct
   invocation, run `uv run scripts/context_bundle.py --intent energy-audit
   --format json`.
2. Retrieve a specific profile section only if the bounded projection omits
   evidence needed for the audit.

## Context Loading

1. **Search the last 14 days of daily notes** with
   `uv run scripts/semantic.py query "physical mental emotional social energy patterns" --path daily-notes --after YYYY-MM-DD --top 10 --context --format json`.
   Triage the capsules, then read only the relevant sections from 3 to 5
   source notes. Report missing evidence rather than loading every daily note.
2. **Search for energy-related notes:**
   - `Grep(pattern: "累|疲惫|精力", path: "$OV/")` — Chinese exhaustion markers (local, exact).
   - `Grep(pattern: "运动|健康|睡眠", path: "$OV/")` — Chinese health markers (local, exact).
   - `Grep(pattern: "exercise|health|sleep", path: "$OV/")` — English health markers (local, exact).
   - `Bash: uv run scripts/semantic.py query "tired exhausted drained" --top 10 --context --format json` — **primary** for affective states that may not use literal "tired/exhausted" language. Reframe the concept and retry if results are thin.

## The Four Energy Dimensions

### 1. Physical Energy
- Sleep quality and quantity
- Exercise frequency
- Nutrition patterns
- Illness or physical complaints
- Weight/fitness trajectory

### 2. Mental Energy
- Focus and concentration quality
- Learning vs. routine work ratio
- Cognitive load from context-switching
- Creative output vs. administrative tasks

### 3. Emotional Energy
- Relationship quality (energizing vs. draining interactions)
- Stress sources
- Joy sources
- Unresolved conflicts or anxieties

### 4. Social Energy
- Introvert/extrovert battery level
- Quality of professional relationships
- Community connection
- Isolation signals

## Energy Matrix

Present findings as:

| Dimension | Sources (+) | Drains (-) | Net Trend |
|-----------|------------|------------|-----------|
| Physical | [what energizes] | [what drains] | ↑ ↓ → |
| Mental | [what energizes] | [what drains] | ↑ ↓ → |
| Emotional | [what energizes] | [what drains] | ↑ ↓ → |
| Social | [what energizes] | [what drains] | ↑ ↓ → |

## Coaching Questions

1. "Which dimension is most depleted right now?"
2. "What's one energy drain you could eliminate this week?"
3. "What energizes you that you're not doing enough of?"
4. "Is any goal consuming disproportionate energy relative to its importance?" (Apply Pareto)

## Output

**File:** `<paths.reflections>/YYYY-MM-DD-energy-audit.md`

```markdown
# Energy Audit — YYYY-MM-DD

## Energy Matrix
[Matrix table from above]

## Key Findings
- [Finding 1 with evidence from notes]
- [Finding 2 with evidence from notes]

## Energy Drains to Address
1. [Drain] — Impact: high/medium/low — Action: [specific]
2. [Drain] — Impact: high/medium/low — Action: [specific]

## Energy Sources to Protect
1. [Source] — currently at risk because [reason]
2. [Source] — underutilized, could increase by [how]

## Recommendations
- This week: [one small change]
- This month: [one structural change]

## Notes Referenced
[All [[Note Title]] links]

## Session Meta
- User engagement: high / medium / low
- Surprise factor: yes / no
```

## Session Log

After writing the energy audit file, emit a session log:
1. `Bash: uv run scripts/session_log.py --type energy-audit --duration <minutes>`
2. `Edit` the created file to populate sections from session data (agents dispatched, searches, questions, frameworks, anomalies). The canonical fill-in guide lives in `protocols/session-log.md` § "Section Guidance". Leave empty sections with headers only. If the write fails, warn and continue.

## Wrap Up

The energy audit file in `<paths.reflections>/` is the durable session output. Daily notes are user-authored; the system reads them but does not modify them. Tell the user the audit has been saved and where to find it.
