# Session Continuity

Defines how sessions connect across conversations so insights compound rather than reset.

## The Continuity Chain

Each session should feel like a chapter in an ongoing conversation, not a standalone event.

### Reading the Thread

Route the request before loading continuity context. Then build a bounded
projection for the selected intent:

```bash
uv run scripts/context_bundle.py --intent "<intent>" --format json
```

The default projection contains only:

1. Profile files named by the intent's `profile_reads` row in
   `harness/intents.toml`.
2. The latest session log's `## Anomalies` and `## Continuity` sections.
3. Headings and high-signal closing sections from up to three recent
   reflection or review files.

Add `--component daily` only when the selected workflow explicitly needs
current capture. Add route-specific sources with `--component sources
--source "<vault-relative-path>[#Section]"`. Generic startup, capture, lint,
sync, promotion, and meeting routes do not load a daily note by default.

The complete serialized projection uses the selected intent's
`context_budget_bytes` from `harness/intents.toml`: 32 KB when the row
preloads profile files, 8 KB for continuity-only rows. Declared sections land
whole, in priority order, until the ceiling; a workflow may raise
`--byte-budget` to at most 64 KB. Anything omitted is reported and retrieved
deliberately after routing. The projection is a "previously on..." anchor,
not a substitute for reading the sources a workflow actually needs.

### Reading recovery

For a reading or transcript/talk route, the projection also includes the most recent Reading
Capsule from a reading session log. This is the bounded recovery path for a
reading whose first analysis completed but whose discussion or reflection did
not. Other routes never preload it.

### Connecting Back

Every session should include at least one reference to a previous session:
- "Last time we talked about X. Has anything changed?"
- "In your last review, Y was flagged as neglected. Let's check in on that."
- "You set an intention last session to Z. How did that go?"

### Carrying Forward

Every session should plant a seed for the next:
- **Next Action:** Specific, actionable, time-bound
- **Open Question:** Something to sit with between sessions
- **Review Date:** When to revisit a decision or check progress

## File-Based Memory

Sessions leave artifacts that future sessions can read:

| File | Purpose | Read By |
|------|---------|---------|
| `<paths.reflections>/YYYY-MM-DD-reflection.md` | Daily insights | Next reflect session |
| `<paths.reflections>/YYYY-MM-DD-review.md` | Goal progress | Next review session |
| `<paths.reflections>/YYYY-MM-DD-weekly.md` | Weekly patterns | Next weekly session |
| `<paths.gtd>/decisions/*.md` | Stable decision records; dated changes live inside each file | Future decision/review sessions |
| `<paths.reflections>/YYYY-MM-DD-exploration.md` | Open threads | Next explore session |
| `<paths.reflections>/YYYY-MM-DD-energy-audit.md` | Energy patterns | Next energy audit |
| `<paths.sessions>/YYYY-MM-DD-<type>.md` | Session process log | Meta-reflection, Evolver, next session (excerpts) |
| `profile/identity.md` | User profile | Intents that declare it in `profile_reads` |
| `profile/directions.md` | Goals | Goal-related intents that declare it in `profile_reads` |

## Cadence Recommendations

| Command | Recommended Frequency | Why |
|---------|---------------------|-----|
| `/project:hi` | Daily or every 2-3 days | Maintains awareness of current thinking |
| `/project:weekly` | Weekly (Sunday or Monday) | Energy and attention patterns need a week of data |
| `/project:review` | Quarterly (full) / Monthly (light pulse via `/weekly`) | Full review is quarterly; light pulse is monthly. Cadence specifics live in `.claude/commands/review.md`. |
| `/project:decision` | As needed | When facing a real decision |
| `/project:explore` | Weekly or when feeling stuck | Keeps serendipity alive |
| `/project:energy-audit` | Monthly or after high-stress periods | Energy patterns are slow-moving |
| `/introspect` | Monthly or after major life change | Profile needs fresh data when context shifts significantly |

## Focus Lock

At the start of each quarter (or era), declare a **primary focus** — the direction and life area getting priority this period. Commitment with friction.

### How Focus Works

1. **Declare at quarterly review or era start:** "This quarter I'm focused on [direction] through [life area]." Example: "Mastery through Career" (proving competence at a new role).
2. **Focus shapes dispatch:** Researcher prioritizes notes related to the focus. Challenger's questions lean toward the focus direction. Scout explores adjacent topics in the focus domain.
3. **Changing focus requires a full `/review` session.** No switching on a whim during a daily reflection. The friction is the feature — it prevents chasing every new interest.
4. **Focus doesn't exclude.** Other directions still get attention. Focus just sets the priority queue.

### Policy Cards

Policies are swappable reflection habits — small commitments that can change when you hit a milestone. Unlike focus (which is quarterly), policies can rotate more frequently.

| Slot | Examples |
|------|----------|
| **Energy** | Morning review, sleep tracking, no-meeting blocks |
| **Capacity** | Weekly deep work sprint, side project hour |
| **Identity** | Weekly journaling, monthly values check |
| **Wildcard** | Current experiment (e.g., deliberate networking, writing practice) |

**Policy swap rule:** You can change a policy for free when you complete a milestone (finishing a goal, hitting a Moment, completing a review cycle). Otherwise, changing a policy mid-cycle should be noted and explained — "I'm swapping [old policy] for [new policy] because [reason]."

## Continuity Anti-Patterns

1. **Groundhog Day**: Every session starts from zero with no reference to previous sessions. Fix: Load the route-specific continuity projection.
2. **Nostalgia Trap**: Spending too long on what was discussed before and not enough on what's new. Fix: One callback, then move forward.
3. **Orphaned Intentions**: Setting "next actions" that never get checked. Fix: Explicitly check last session's intention at the start.
4. **Overloaded Sessions**: Trying to do reflect + review + weekly all at once. Fix: One command per session.
