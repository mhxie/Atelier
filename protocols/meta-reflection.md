# Meta-Reflection Protocol

Reflect on the reflection process itself. This is how the system becomes self-aware of its own quality and evolves deliberately.

## When to Run

- After every 5th session: the headless draft routine below covers this
- When session scores trend downward for 3+ sessions
- When the user explicitly asks "how is the system doing?"
- During Evolver runs

## Headless draft

The "after every 5th session" trigger used to be a session-start question
("run it now?"). It is now a draft the system produces on its own; the human
reviews a document instead of authorizing a run.

- **Routine**: `meta-reflection-draft`, `execution = "local"`, profile
  `local-synthesis`, weekly (Sunday early morning), invoked as
  `/run-routine meta-reflection-draft` from its private prompt under
  `<paths.routine_prompts>/`.
- **Due rule**: at least 5 session logs in the trailing 30 days. Otherwise the
  routine writes a one-paragraph no-op artifact at the same path pattern
  (so the run is verifiable) and returns `noop`.
- **Output**: `<paths.agent_findings>/meta-reflection/<YYYY-MM-DD>-meta-reflection-draft.md`,
  the Step 5 report plus the Step 6 audit answers, with `## Evolution
  Applied: pending review` and every prescription phrased as a proposal.
  Read-only over sessions, reflections, and the harness; the draft is the
  only write. It never edits protocols, agents, or commands.
- **Bounds**: single pass, about 60K tokens, idempotent (an existing draft for
  the run date is left alone; the run reports `noop`).
- **Review**: the draft appears under the routine-outputs cue like any other
  routine file. Reading it and acknowledging in `routine_acks.json` closes
  the loop; applying a prescription is an ordinary harness change through
  `protocols/evolution.md`. `scripts/cues.py check_meta_reflection_due`
  fires only when the draft is missing, which points at the routine.

## The Meta-Reflection Process

### Step 1: Gather Session Data
Read session logs from the last 5-10 sessions in `<paths.sessions>/`:
- Agent dispatch patterns (which agents run, how many turns they use)
- Search effectiveness (hit rates, useful-signal rates from Search Log)
- Gate pass rates (from Gate Results)
- Question landing rates (from Questions & Engagement)
- Framework fit scores (from Frameworks Applied)
- Anomaly frequency and types
- Harness assumptions exercised

Also read reflection outputs from `<paths.reflections>/`:
- Score cards (overall scores, per-dimension scores)
- Session meta (engagement levels, questions that landed)
- Patterns identified
- Frameworks used

### Step 2: System Health Assessment

| Dimension | Health Check |
|-----------|-------------|
| **Research Quality** | Are searches returning relevant results? Gap frequency? |
| **Synthesis Depth** | What insight levels are being achieved? (Summary vs. Implication) |
| **Question Quality** | Are Challenger's questions getting engaged responses? |
| **Framework Fit** | Are frameworks being applied specifically or generically? |
| **Continuity** | Are sessions connecting to each other? |
| **User Engagement** | Are responses getting longer/shorter? More/less thoughtful? |
| **Note Coverage** | Are we surfacing notes from across the archive or just recent ones? |
| **Epistemic Independence** | Is the ratio of AI-tagged to user-written content in daily notes shifting? Are AI write-backs becoming the primary record? |
| **Search Effectiveness** | Are semantic.py queries returning useful results? Hit rate trend across sessions? |
| **Agent Utilization** | Are all agents being used? Any chronically idle? Any over-dispatched? |
| **Gate Health** | What percentage of sessions pass Gate 3 on first try? Revision loop frequency? |

### Step 3: Identify System Bottleneck

Apply Theory of Constraints to the system itself:
- What is the ONE thing limiting session quality right now?
- Is it search quality? Synthesis depth? Question relevance? Framework selection?
- Focus improvement on the bottleneck, not everything at once.

### Step 4: Prescribe Evolution

Based on the bottleneck:

| Bottleneck | Evolution Target | Action |
|-----------|-----------------|--------|
| Weak search results | Researcher prompts | Improve query patterns |
| Generic synthesis | Synthesizer patterns | Add new insight patterns |
| Questions not landing | Challenger taxonomy | Adjust question types |
| Frameworks feel forced | Thinker selection | Improve selection criteria |
| Sessions don't connect | Continuity protocol | Strengthen reading chain |
| Low engagement | Coaching style | Adjust tone in CLAUDE.md |
| Stale index | Index command | Add incremental refresh |

### Step 5: Document & Track

Write a meta-reflection report:

```markdown
# Meta-Reflection — YYYY-MM-DD

## Sessions Reviewed: N (date range)

## System Health
| Dimension | Score | Trend | Notes |
|-----------|-------|-------|-------|
| Research | X/10 | ↑↓→ | |
| Synthesis | X/10 | ↑↓→ | |
| Questions | X/10 | ↑↓→ | |
| Frameworks | X/10 | ↑↓→ | |
| Continuity | X/10 | ↑↓→ | |
| Engagement | X/10 | ↑↓→ | |
| Search Effectiveness | X/10 | ↑↓→ | |
| Agent Utilization | X/10 | ↑↓→ | |
| Gate Health | X/10 | ↑↓→ | |

## Bottleneck: [identified constraint]
## Prescribed Evolution: [specific action]
## Evolution Applied: [what was changed, or pending]
```

## Step 6: Governing Variables Audit (Double-Loop)

Single-loop asks "how do we improve this session?" Double-loop asks "are we measuring the right things and questioning the right assumptions?" Every meta-reflection must answer these three questions to prevent the system from optimizing confidently in the wrong direction.

1. Rubric validity: Is the scoring rubric measuring what matters? Read the last 5 session metas. Are high-scoring sessions actually the ones the user found most valuable? If scores and user engagement diverge, the rubric needs recalibration before any other evolution.
2. Ontology completeness: Were there any session anomalies that didn't fit the symptom-source table in evolver.md? If yes, the table needs updating before any other evolution, because unclassifiable failures repeat silently.
3. Complexity audit: Count protocols, agents, frameworks. Compare to the trailing-30-day session count (`uv run scripts/session_stats.py --json`). If protocols exceed 1.5x that count, or `harness_lint` fires `prose-budget`, trigger a pruning review before any additive changes, because the system's default failure mode is monotonic growth.
