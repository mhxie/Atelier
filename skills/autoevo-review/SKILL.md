---
name: autoevo-review
description: Triage the pending queue produced by the Autoevo routine.
---
# /autoevo-review

Triage pending Autoevo proposals with the user: apply, skip, defer, or explain.
Follow `protocols/autoevo.md`. This is an interactive approval workflow, not a
continuation of the nightly model run.

Never edit canonical nightly JSON or its derived reports during review; doing so
cannot prove an incomplete cycle. Optional human history belongs in a separate
`<paths.agent_findings>/autoevo-review-<YYYY-MM-DD>.md`.

## 1. Load and housekeep

Resolve `<paths.meta>/autoevo_pending.toml` and the current local date once.
Load through `scripts/autoevo_pending.py list --status pending`; a missing queue
or zero returned entries means there is nothing to review. A malformed queue is
a blocker: report it and do not rewrite it.

Before presentation, stage `autoevo_pending.py auto-dismiss --today <date>` on
private state for entries at three surfaces or over the default 30-day age.

Never mutate live state directly. After approval, the orchestrator writes
explicit note, queue, ledger, and optional review-log changes as plain files
after rechecking their hashes; Reflect commits and pushes them.

## 2. Present the queue

Group remaining entries by `redundant`, `time-stale-A`, `time-stale-B`,
`contradicted`, and `low-signal`. Show a compact category count followed by each
entry's id, action, evidence, peers, proposal date, surface count, and any
`default_action` / `default_at`.

Explain veto semantics exactly:

- Skip vetoes a `stale-banner` default.
- Apply vetoes a `dismiss` default; skip confirms it.
- Defer restarts the 14-day window.
- Silence leaves an eligible default to the later trusted nightly parent.

Let the user choose a category or all entries. If unclear, walk all entries in
order. For each item ask: apply, skip, defer, explain, or quit. Apply and skip
require one user-authored sentence of reason; never invent or accept an empty
reason. Defer may carry an optional reason.

## 3. Resolve one item

### Apply

Dispatch the relevant normal, user-approved workflow. Curator drafts note
changes and retains its full preservation/approval gates. Autoevo's
`auto_apply_safe` flag does not bypass approval here.

A queued contradiction is not necessarily confirmed: the nightly queues both
genuine and unproven contradictions. Unless the entry carries a complete,
gap-free Challenger verdict tied to the exact claim and peer, dispatch
Challenger first. A genuine verdict may proceed to Curator's normal wiki-update
proposal; a rhetorical verdict performs no wiki write and returns to the user
for a dismissal decision.

After approval, stage `autoevo_pending.py resolve --id <id> --status applied
--reason <reason> --today <date>` on private state. Write it with the approved
note change. Archive means byte-preserving addition plus deletion, not a model
write or promised rename.

If the user rejects the proposal, stage a `dismissed` resolution with their
reason instead. No content change is written.

### Skip

Stage `autoevo_pending.py resolve --id <id> --status dismissed --reason
<reason> --today <date>` against private state. Tell the user whether this
vetoes `stale-banner` or confirms `dismiss`; then include it in the next queue
write.

### Defer

Stage `autoevo_pending.py defer --id <id> --today <date>` with the optional
reason. It increments surfacing state and, when applicable, restarts the default
deadline. Optionally ask for a cue snooze (default seven days); cue snoozing is
group-wide, not per entry. Batch compatible defer-only state changes into one
write.

### Explain

Show the complete stored evidence without adding authority. For redundant QMD
findings, show peer paths, QMD ordering scores, and source-reviewed overlap; do
not describe a score floor. For contradictions, show the claim, peer, signal,
probe verdict, and any unresolved gaps. Then ask again.

### Quit

Stop. Write only already confirmed staged changes; leave every other entry
pending. If writing has not begun, discarding scratch state has no live effect.

## 4. Write and summary

Before writing, recheck live hashes and limit the write to approved
note/state/review-log paths. Never commit or push. Rollback is plain
`git restore`.

Report applied, dismissed, deferred, remaining, and any failed write.
Name the queue path and separate review log if one was created. A review log is
ordinary human-triage history only; it is never an Autoevo verifier input.

## Edge cases

- Missing peers or changed evidence make an entry stale. Explain the mismatch
  and ask the user whether to dismiss; do not silently resolve it.
- A wiki change always uses Curator's normal approval path and appends the
  required Revision Log.
- Any daily-note source is a nightly bug. Refuse the content operation and ask
  the user whether to dismiss the queue entry; daily notes remain read-only.
- On ambiguous or partly written effects, stop and report `needs_review`.
  Do not replay the model, automatically roll back, or overwrite state.
