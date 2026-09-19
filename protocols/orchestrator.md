# Orchestrator Protocol

The main agent owns scope, integration, and the user-facing result. Delegate
bounded outcomes, not individual tool calls; do not become every worker's
command runner. Roles and dispatch responsibilities live in `protocols/agent-handoff.md`.

## Primitive Selection

Extend an existing owner before adding a primitive. A one-off stays inline.

| Need | Primitive |
|---|---|
| Repeatable deterministic work | Existing script; no model worker required |
| Deterministic lifecycle/tool check | Hook calling the script; verify failure behavior |
| Repeated user workflow | Command with the runtime's skill/entry adapter |
| Independent investigation, judgment, or separable implementation | Agent with bounded context and ownership |

Entry syntax and host differences belong to `protocols/runtime-adapters.md`.
A separate agent context is not an OS isolation boundary.

## Coordination

The selected procedure owns dispatch. Use `protocols/agent-handoff.md` →
Dispatch Contract for each assignment and only the matching return schema.
`protocols/agent-handoff.md` → Responsibilities and Voice Legs owns model/voice
mechanics, not task authority.

## Session Startup Checks

Route before loading personal context. After selecting the intent, run
`scripts/context_bundle.py --intent <name>` when the row declares profile reads. Use
that Repomix artifact as the shared startup context; do not separately reread
the same profile or session files.

1. **Era state:** Only when the route declares `directions.md` in
   `profile_reads`, use its packed `## Current era` material for the era,
   directions, and quarterly focus. Pass the excerpt to Synthesizer and
   Challenger.
2. **Focus Lock:** Only goal-related routes apply the declared focus.
   Researcher prioritizes its domain and Challenger leans questions toward it.
   Changing focus requires a full `/review` session.
3. **Profile freshness:** The helper checks `Last built:` only for profile files
   selected by the route. If one is older than 7 days, suggest `/introspect`.
   Routes with empty `profile_reads` do not inspect profile freshness.

The selected intent declares a Repomix `o200k_base` token ceiling in
`harness/intents.toml`. Overflow fails after packing and is never auto-trimmed.
Daily capture and full source files are explicit `--source` additions, not
generic startup context. The complete contract is in
`protocols/session-continuity.md`.

## Criteria-First Dispatch

Before multi-step work, state the outcome and observable verification. Clarify
interpretations that materially change scope or authority; otherwise state a
reasonable assumption and proceed. For example: "Compact the selected notes
without losing claims or media; verify snapshots, integrity checks, and the
user-approved output." Task-specific steps belong in the selected procedure.

## Runtime Conflict Surfacing

Distinguish instruction conflicts from disagreements about evidence:

- Follow the runtime's instruction hierarchy. A dispatch cannot expand user
  authority or waive shared invariants. Report an unresolved instruction or
  authority conflict with both sources; stop the affected action and let the
  parent clarify it, continuing independent safe work when possible.
- Workers and reviewers may investigate contradictory evidence within scope.
  Return the sources, checks, and supported conclusion; escalate unresolved
  contradictions instead of averaging verdicts or guessing.

## Note Writing

`AGENTS.md` owns vault write authority; delegation does not change it.

- Curator drafts cognitive note operations; the parent validates the proposal
  and writes to its `target_path` under the selected procedure's approval or
  existing Autoevo authorization. Never target daily notes.
- Scribe directly records user-authored raw content at the assigned target;
  the parent does not transcribe it. `protocols/intent-capture.md` owns capture
  routing, including `/dine` Intent C's confirmation for trip-associated meals.
- Operational logs use only the bounded schema in `protocols/session-log.md`;
  they cannot substitute for approved notes. Autoevo retains only its
  procedure's existing unattended authorization.

## Reader → Scholar auto-promotion

The selection rule lives in `skills/read/SKILL.md` → Reader vs Scholar selection. The selected
reading worker may apply several requested lenses; difficulty does not imply
additional agents.

## Session Flow

1. Gather through the selected `harness/intents.toml` route and procedure;
   parallelize only independent tasks.
2. Synthesize bounded returns in the parent. Add Synthesizer only for substantial
   separate analysis; add Challenger only for a named reasoning uncertainty or
   an explicit procedure gate.
3. Validate completion, evidence, and the selected quality gates. Reuse valid
   checks; do not rerun every worker action merely because it was delegated.
4. Present the integrated outcome, actual contributors, and evidence limitations.
   Facilitate follow-up questions and authorized actions without dumping agent logs.

## Action Routing

`harness/intents.toml` selects the procedure; that procedure owns dispatch.
Use the selected role's brief and matching `protocols/agent-handoff.md`
contract for operation fields and write boundaries.
