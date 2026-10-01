# Agent Handoff Protocol

Owns dispatch inputs and return contracts. Load the common sections and only the
selected payload contract, not the entire catalog. The selected procedure owns
which roles run; a handoff is not authority to add another agent or write.

## Dispatch Contract

Give each worker one bounded outcome and these six items. Reuse supplied context
by reference; do not paste shared policy or repeat the parent's full history.

| Item | Required meaning |
|---|---|
| Objective | Result or question the worker owns, not a list of tool calls |
| Scope | Relevant sources and exact file/module ownership; identify concurrent work |
| Allowed effects | Read/write/network boundaries within existing user and procedure authority |
| Done when | Observable acceptance checks and required evidence, including the relevant baseline |
| Budget | Applicable time, turns, or token bound; reserve room to report incomplete work |
| Escalate when | Missing prerequisites, unresolved authority conflicts, or required scope expansion |

Workers choose their in-scope reads, commands, and tests without returning each
tool call to the parent. Disjoint ownership permits parallel implementation;
preserve others' edits. Reviewers independently investigate but do not modify the
reviewed bundle. Tool availability does not grant authority, and a role prompt
does not enforce OS isolation. Check actual runtime boundaries before relying
on confinement; unavailable required enforcement is a gap, not permission to bypass.

Example: verify that a runner cannot write to the wrong target. Read the runner
and callers, run offline tests with disposable fixtures under the available
execution boundary, and return a reproduction plus uncovered cases. Do not edit
reviewed sources or real data; escalate if those effects become necessary.

## Envelope Format

Every inter-agent return loads this section and its selected contract, then
include the common metadata below plus that payload. The dispatch selects the
recipient; a payload type does not schedule a named downstream role. Use the
selected contract's markers when specified (notably Forgetter); otherwise use
these markers:

```
---handoff---
from: <agent name>
to: <agent name>
type: <selected contract type>
confidence: high | medium | low
completion_status: complete | partial | aborted
remaining_work: <unfinished required work; empty only when complete>
gaps: <comma-separated list of what's missing>
---end-handoff---
```

`complete` means the assigned outcome and required checks are complete; it does
not mean the result is favorable. Use `partial` for skipped, deferred, or degraded
work, and `aborted` when stopping early, including a midpoint scope conflict.
`remaining_work` lists unfinished requirements; `gaps` lists evidence sought but
unavailable. Both may apply. Missing completion metadata is never a pass: obtain
a corrected return or report incomplete coverage, without assuming completion.

Payload fields below extend the common metadata. The parent inspects evidence
and limitations before accepting a result; it does not blindly forward aborted
work as complete input to synthesis. A finding can remain useful in a partial
return, but cannot stand in for unperformed verification.

## Contract: Researcher → Synthesizer

**Type:** `research-brief`

Payload: the Research Brief body in `agents/researcher.md` → Output
Format (query, search strategy, sources with edit dates, verbatim excerpts with
language, patterns, gaps). The Synthesizer does not re-search; critical gaps
escalate to the orchestrator.

## Contract: Reader → Synthesizer

**Type:** `reader-brief`

Payload: the `---reader-brief---` body in `agents/reader.md` → Output
Format (`lens`, `source`, then per lens the findings, quotes, cross-signals for
other lenses, and one-sentence verdict). The parent normally receives this
brief; a separate Synthesizer is optional. Preserve convergence, disagreement,
and evidence limitations when combining briefs.

## Contract: Synthesizer → Reviewer

**Type:** `synthesis`

Payload: `output_type` (reflection | review | exploration | reading-report)
and the matching body in `agents/synthesizer.md` → Output Formats,
ending with its Source Audit (sourced claims, unsourced observations, goals
referenced and missing).

## Contract: Reviewer → Orchestrator

**Type:** `review-check`

Required fields (envelope shape only; verdict rules are canonical in `agents/reviewer.md` → Scoring, do not duplicate here):

- `verdict`: `"APPROVED" | "APPROVED_WITH_NOTES" | "NEEDS_REVISION" | "REJECTED"`, with a one-line summary
- `dimensions`: one compact read per Session Review dimension (`citation_accuracy`, `goal_coverage`, `honesty`, `staleness`, `synthesis_quality`), each `{read: "", issues: []}`; a dimension the selected mode skips is `n/a`
- `findings`: Array of `{severity: BLOCKER | SHOULD-FIX | NICE-TO-HAVE, location, mechanism, correction}`
- `coverage`: What was inspected or tested, and anything required but unavailable
- `residual_risk`: Remaining risk or an important limitation of the reviewed output

## Contract: Challenger → User

**Type:** `challenge-set`

Payload: the Challenger's Questions body in `agents/challenger.md` →
Output Format (grounding, emotional register, affirming, probing, and
challenging questions, the one question, framework note).

## Contract: Thinker → Orchestrator

**Type:** `perspective`

Payload: the Independent Perspective body in `agents/thinker.md` →
Output Format (reframe, framework with applicability 0-10, cross-validation,
contrarian take, external signal).

## Contract: Scout → Orchestrator

**Type:** `scout-brief`

Payload: the `---scout-brief---` body in `agents/scout.md` → Output
Format (topic, assigned direction, sourced findings with dates, contrarian
signal, recent developments, knowledge gap). Bounded verification returns the
claim, source locators, excerpts, and unresolved limits instead.

## Contract: Librarian → Orchestrator

**Type:** `recommendation`

Payload: the Summary View in `agents/librarian.md` → Output Format
(topic, triggering context, resources with author, type, core insight, and
relevance to the user), plus which resources were excluded as already read.

## Contract: Orchestrator → Curator (Compact/Merge Dispatch)

Before compact/merge dispatch, the parent snapshots every source to
`<paths.cache>/<operation>-<slug>.md` using its relative path slug. Supply these
paths as `snapshot_paths`; Curator reads only those snapshots, never mutable
originals. All snapshots must exist before drafting begins.

**Auto-apply mode (autoevo nightly only).** Instead, supply original
vault-relative identities and exact, equally ordered `snapshot_paths` from the
retained `plan.source_files` mapping. Curator reads only those workspace files
and refuses any missing, empty, substituted, reordered, or extra snapshot. An
archive has exactly one identity/snapshot and preserves its content unchanged.

- `mode`: `auto-apply`
- `band`: `redundant-high` | `low-signal-high`
- `evidence`: complete triggering Forgetter row, including `confidence: high`

The trusted parent, not Curator or the nightly model, rechecks live sources and
publishes any accepted change.

## Contract: Orchestrator → Challenger (Probe Contradiction)

Used by the `autoevo-nightly` routine to distinguish rhetorical contradictions from
genuine ones before routing them. Read-only by contract; Challenger does not
write any file. Only a complete, gap-free rhetorical verdict dismisses a
finding. Genuine and unproven findings are queued for human review.

The dispatch prompt includes:
- `task`: `probe-contradiction`
- `wiki_claim`: full text of the L4 wiki claim Forgetter flagged
- `contradicting_peer`: relative path under `$OV/` of the L2 note containing the apparent contradiction
- `contradiction_signal`: the exact phrase from the peer that Forgetter flagged as correction-language

Response envelope:
- `from`: `challenger`
- `to`: `orchestrator`
- `type`: `contradiction-probe`
- `verdict`: `genuine` (the peer really overturns the wiki claim) | `rhetorical` (the "actually" / "wrong" / "事实上" is rhetorical or refers to a different referent)
- `rationale`: one short sentence retained with the parsed probe. The parent
  uses it as structured routing evidence or a proposal note; Challenger does
  not write a queue entry or report.

## Contract: Curator → Orchestrator

**Type:** `note-operation`

Use `---curator-proposal---` / `---end-proposal---` with common metadata.

Required fields:
- `operation`: compact | merge | create | replace | wiki-entry | archive
- `mode`: `normal` (default; user-approval gate applies) | `auto-apply` (only valid for compact/merge/archive ops dispatched by the Autoevo routine)
- `band`: required iff `mode = auto-apply`; one of `redundant-high` | `low-signal-high`. Omitted otherwise.
- `auto_apply_safe`: required iff `mode = auto-apply`; `true` when Curator's scope guards pass and the content-preservation checklist succeeded, `false` otherwise.
- `refusal_reason`: required iff `auto_apply_safe = false`; one short sentence explaining which guard tripped. Orchestrator surfaces this to the pending queue.
- `source_notes`: Source note titles for normal compact/merge. In Autoevo, use
  the exact ordered original vault-relative source identities from dispatch.
- `source_path`: Required for archive; the original path under the authorized working tier.
- `target_path`: Required for `wiki-entry` and `archive`, optional otherwise. Curator proposes; the parent publishes only after approval or trusted Autoevo validation.
- `proposed_title`: Human-readable title of the proposed note.
- `snapshot_paths`: Required for compact/merge/archive. Normal mode uses cache snapshots; Autoevo requires the ordered workspace paths from its retained plan.
- `media_inventory`: (required for compact/merge, omit for create/replace/archive) `{images: count, tables: count, structured_blocks: count, embeds: count}` — counts from source notes. The orchestrator verifies these counts match the output.
- `media_output_count`: (required for compact/merge) `{images: count, tables: count, structured_blocks: count, embeds: count}` — counts in the proposed output. Must match `media_inventory` or differences must be listed in `changes_summary`.
- `external_content`: (required for compact/merge, omit for create/replace/archive) List external material found in the sources; every item must remain clearly attributed in `proposed_content`. Use an empty list when none exists.
- `proposed_content`: New/merged content; archive preserves the supplied snapshot unchanged for byte-preserving addition plus source deletion.
- `estimated_size`: Approximate byte size of `proposed_content`. If >15KB, must include a split plan.
- `content_integrity`: (required for compact/merge, omit for create/replace/archive) `{verbatim_preserved: boolean, structures_preserved: boolean, images_preserved: boolean, checklist_passed: boolean}` — self-assessment that the Content Preservation Checklist was run
- `changes_summary`: Material added, removed, or merged. Any omission names the exact content and reason.
- `post_write_action`: Normal compact/merge only: the user's archive/delete choice. Omit for Autoevo.
- `rationale`: Why this operation was recommended

## Contract: Forgetter → Orchestrator

**Type:** `decay-report`

Forgetter returns findings inline. Autoevo validates and normalizes them into
the candidate `proposal.json`; after trusted publication, human-readable sweep
reports are derived from the canonical JSON domain result. Forgetter never
writes or edits a report.

Required fields:
- `from`: `forgetter`
- `to`: `orchestrator`
- `type`: `decay-report`
- `mode`: `full` (sweep ran to completion) | `partial` (sweep stopped early on `max_candidates`, `time_budget_s`, or to keep room for the envelope before the turn ceiling)
- `summary`: `{redundant: N, time_stale: N, contradicted: N, low_signal: N}` — counts per category
- `findings_inline`: Full categorized findings keyed by category. Each row uses
  the evidence required by `agents/forgetter.md` category sections:
  - `redundant`: `{path, confidence, peers, scores, mode: "qmd", evidence, proposed_action}`
  - `time_stale`: `{path, confidence: "medium", heuristic, evidence, proposed_action}`
  - `contradicted`: `{wiki, claim_id, confidence: "low", peer, signal, proposed_action}`
  - `low_signal`: `{path, confidence, words, links_in, tags, mtime, proposed_action}`
- `sweep_notes`: Tool-call count, duration, retrieval mode, caps reached, and read or scope gaps.

`findings_inline` is required on every successful return (whether `mode: full` or `mode: partial`).

The envelope markers are exactly `---forgetter-result---` and
`---end-result---`. No other opening marker is valid.

**No-envelope case (out-of-band).** If Forgetter is interrupted and never emits
the closing `---end-result---` marker, the nightly proposal records that exact
scope as `outcome: forgetter_no_envelope`, `mode: absent`,
`completion_status: aborted`, with no accepted findings and an explicit error.
Canonical result JSON and Prefect state are the status/cue evidence; no consumer
parses a Markdown Errors section as domain truth. If the outer proposal is
missing, the adapter fails the Prefect run without publishing a live report.

**Per-finding `confidence` field.** Every row in `findings_inline` carries
`confidence: high | medium | low` derived per category by the rules in
`agents/forgetter.md` § Confidence Field. Confidence is evidence, not
write authority: the trusted parent routes and rechecks every finding against
its retained plan and live state. A missing confidence is normalized to medium
and cannot auto-apply.

**Cross-reference:** `agents/forgetter.md` owns heuristics and
confidence; this protocol owns the envelope, and `scripts/autoevo_verify.py`
owns derived report rendering.

## Contract: Meeting → Orchestrator

**Type:** `meeting-notes`

Payload: a one-line source description (meeting name, date, participants if
known) followed by the structured body in `agents/meeting.md` → Output
Format (key takeaways, action items by owner, decisions, next steps, items
flagged unclear). Envelope `confidence` reflects how clean the transcript was.

The orchestrator presents the structured notes to the user and asks whether to create a local note via Curator.

## Responsibilities and Voice Legs

| Owner | Responsibility |
|---|---|
| Parent | Sets scope and ownership, selects the procedure, integrates evidence, and obtains any new authority from the user. Synthesizes bounded returns itself. |
| Worker | Owns the assigned outcome; reads, diagnoses, and resolves evidence disagreements within its scope and write boundaries. Returns results or a concrete blocker, not every intermediate decision. |
| Reviewer | Independently checks the assigned change or claim, including relevant callers and failure paths. Does not implement fixes or grant authority. |
| Scripts | Execute deterministic checks and declared operations. Their output is evidence, not permission. |

Default to the parent plus one worker with a bounded area of ownership; known
small tasks stay inline. Add workers only for separable work, a named evidence
gap, or independent judgment. Only a selected procedure can authorize an
additional dispatch, so a worker escalates missing authority instead of
granting it to itself.

Each role's `voices` table in `harness/agents.toml` maps a leg name to a model
identity. A `native` leg runs the role file in the selected runtime; a `direct`
leg runs `uv run scripts/chat_completion.py --model <identity> --max-tokens 0
--prompt -` with the prompt on stdin; an `agy` leg runs `uv run
scripts/agy_leg.py` (web search, no file or command tools). Prefer `agy` as the
second leg when available, else `direct`. A missing key, binding, or CLI exits 2.
Soft-skip an unavailable leg, never silently: report the downgrade, e.g.
`Cross-provider check downgraded: <role> ran native-only (<reason>).` Never
claim a plan completed when a planned leg did not run.

## Escalation Protocol

If any agent encounters:
- **Empty search results**: Try alternative queries (synonym, other language, broader terms) before reporting gap
- **Contradictory evidence**: Flag explicitly — don't resolve silently
- **Token budget exceeded**: Summarize and note truncation
- **Drift from the assigned criterion**: if the work in hand no longer serves the original objective, stop, set `completion_status: aborted`, and return what is verified and what remains as the envelope payload. The orchestrator decides whether to redispatch with a tighter criterion or accept the partial result.

## Revision Loop

The implementation owner fixes findings; the reviewer verifies them without
editing the reviewed bundle.
