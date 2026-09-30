## Purpose and authority

Autoevo is a Prefect-owned decay sweep over the working tiers. Forgetter
supplies findings; trusted policy selects eligible operations, preserves
uncertain findings in the pending queue, and publishes one local Git commit
per operation. It produces derived decay/audit reports, not synthesis or
reflection.

This is the narrow auto-commit exception to `protocols/repo-conventions.md`.
It never auto-pushes. Wiki, localized wiki shadows, daily notes, and user
edits remain protected. The model proposes; only the trusted parent writes
the live vault.

## Execution boundary

`protocols/remote-routines.md` owns Prefect scheduling, concurrency, and retry
timing. The cycle is its validated scheduled local date. A reviewed manual
run still enters through Prefect. Direct invocation of the nightly command
without a trusted plan refuses; it must not launch another runtime.

The trusted adapter performs these phases:

1. Run read-only `scripts/autoevo_preflight.py`. A blocked result goes to
   Prefect state and its cue, without model work, a live audit, or a commit.
2. Retain an authoritative plan and stage its model-readable `plan.json`
   plus clean tracked source snapshots under `AUTOEVO_WORKSPACE`. The model
   cannot change authorization by editing its copy.
3. Run the model with live `$OV` read-only. The only authored deliverable is
   workspace `proposal.json`; policy preview may write explicit workspace
   scratch. QMD uses a staged derived database and the original cached
   models read-only. No download, index update, or inline rebuild.
4. Validate the proposal against `autoevo_verify.proposal_schema(plan)`;
   recheck readiness, HEAD, state hashes, source hashes/mtimes, protected
   paths, trust bands, tombstones, and Curator safety before publishing.
5. Publish through `autoevo_commit.publish_changes`, update the queue,
   quarantine and decision ledger through their deterministic owners, run
   lint, derive reports, and verify the structured result against Git.

The model never writes the live queue, ledger, quarantine, reports or notes;
runs commits; repairs Git; or performs lint/finalize. The routine adapter owns its
judgment work, not the parent phases above. Runtime permissions and time
limits remain in `harness/routine_profiles.toml` and
`protocols/runtime-adapters.md`.

### Read-only preflight

| Gate | Requirement |
|---|---|
| Git worktree, branch and index | Existing index on the checked-out default branch; a missing index is not ordinary dirtiness. Default is origin's declaration, or an unambiguous local main/master. Never switch branches automatically. |
| Git lock/operation | No `index.lock`, merge, rebase, cherry-pick, revert, or bisect in progress. Never remove locks or repair Git automatically. |
| Session lock | No fresh `<paths.cache>/atelier-session-lock` within six hours. |
| Managed state | No dirty `_meta/autoevo_*.toml`; dirty working-tier content instead becomes `protected_paths`. |
| Privacy | No public-bound privacy hits. |
| Semantic readiness | QMD status reports ready with stored documents/vectors and cached models. This is not live inference or corpus-freshness proof. |
| Legacy audit state | An old owned-audit marker requires explicit review/migration; never read, delete, or commit its referenced audit automatically. |

Privacy and semantic input checks run before drafting. Publication rechecks
the live Git, session-lock, managed-state and protected-content gates; it does
not repeat retrieval or public-harness scans for each operation.
Transient inspection failures return structured
defer evidence. A later Prefect occurrence may retry a pre-model block;
failed, pending, or uncertain post-model effects never authorize model replay.

Interactive SessionStart/UserPromptSubmit hooks refresh the session lock.
The scheduled adapter suppresses its own lock touch. An absent lock means no
recent session; an unreadable lock is not permission to bypass the gate.

## Proposal and result ownership

`scripts/autoevo_verify.py` owns the proposal schema, structured result and
report rendering. The proposal contains exactly the planned sweep identities,
findings, completion evidence, hash-bound precedent judgments, notes and
errors. Confidence is evidence, not write authority. The parent retains its
original plan and independently routes every finding.

A full/complete Forgetter envelope counts as returned coverage. A valid
partial/partial envelope also counts, with its cap reason and unfinished
work recorded in Notes; only independently supported findings may proceed.
A missing envelope supplies no accepted findings and is an error, never a
successful empty sweep. An invalid or aborted return cannot authorize an op.

Canonical domain evidence is
`<paths.meta>/routine_receipts/autoevo-nightly/<cycle>.json`. It contains
the retained plan, proposal, operation intents/commit evidence, deterministic
lint and report mapping. Human-visible `autoevo-applied-<cycle>.md` and decay
reports are derived views, not inputs parsed back into machine truth.
Prefect owns execution state and pre-model-block cues.

`autoevo_verify.py --cycle <YYYY-MM-DD> --vault <vault> --json` requires a
complete matching result, at least three returned sweeps, no coverage/errors
or newly introduced lint errors, exact Git evidence for every operation,
and a final audit publication matching the derived reports. Quarantine skips
or missing sweeps cannot masquerade as a clean cycle.

## Trust bands

### Auto-apply

The numbers below render `scripts/autoevo_run.py` `BAND_RULES`;
`harness_lint.py` checks agreement. Do not repeat them in command prose.
Trusted routing re-verifies scores, tiers, mtimes, mode and content.

QMD findings carry `mode: qmd`. QMD scores
are uncalibrated ordering signals. Redundant QMD findings require content
review and human approval regardless of score.

| Category | Threshold | Op |
|---|---|---|
| Redundant | 3+ peers ≥ 0.85 retrieval AND all peers + candidate in `<paths.wip>/` AND all untouched > 30d AND mode `real` | Complete safe Curator merge; trusted parent publishes band `redundant-high`. |
| Low-signal | All 5 Forgetter conditions hold AND untouched > 365d | Complete safe Curator archive; parent moves the unchanged source into `<paths.archive>/decayed/`, never unbacked deletion. |
| Contradicted (rhetorical) | Complete Challenger probe says rhetorical, not a real contradiction | No op; explanatory note only. |

Curator must return complete metadata, no remaining work, the matching band,
`mode: auto-apply` and explicit `auto_apply_safe: true`. Merge sources come
from the trusted clean snapshot; the oldest source is the survivor. Preserve
verbatim content, attribution, language, images, tables and structured blocks.
Missing content, mismatched media inventories, or a required split blocks
automatic merging. A refusal remains pending, not inferred permission.

### Default after a veto window

Precedent requires at least three concordant past human decisions of the
same class and all gates in `protocols/decision-ledger.md`. The model's
judge response is bound to the preview bundle hash; the parent rebuilds the
bundle and rejects stale or unknown judgments before setting a default.

- `dismiss`: resolve in place after the window.
- `stale-banner`: only time-stale-A with eligible working-tier peers under
  `<paths.wip>/` or `<paths.research>/`. The executable default marks an
  eligible source stale, never performs the user's proposed research action.
- `default_at = today + 14d`. Skip vetoes stale-banner; apply vetoes dismiss.
  Skipping dismiss confirms it. Defer restarts the window.
- Unbacked entries remain human-only. Fixed `append --rule-defaults` is off
  by default. Expired defaults recheck source/state and tombstones; changed,
  vetoed, deferred or resolved entries do not fire.

### Pending and protected content

| Category | Pending rule |
|---|---|
| Redundant | QMD: 3+ distinct working-tier peers with source-reviewed overlap, no score floor. Legacy calibrated evidence keeps its 0.6 floor. Subject documents never count as duplicates. |
| Time-stale-A / time-stale-B | Always queue; era judgments never auto-act, content-stale defaults require precedent. |
| Contradicted (genuine or unproven) | Queue; wiki rewrites require human approval. An incomplete probe cannot dismiss a finding. |
| Low-signal | Five conditions hold and 90–365d untouched. |

Only `<paths.wip>/`, `<paths.research>/`, and `<paths.reflections>/` are
sweep/source tiers. `<paths.agent_findings>/` is report output, never a sweep
target. Wiki, localized wiki shadows and daily notes never auto-apply.
Untracked, dirty, changed, symlinked or otherwise unauthorized sources cannot
become automatic operations. Retrieval context is not source authorization.

## Pending queue and decision history

`scripts/autoevo_pending.py` owns
`<paths.meta>/autoevo_pending.toml`; never hand-edit its TOML during a run.
The parent stages state changes privately and publishes them only after
rechecking the live originals.

- Deduplicate sorted peer sets already pending or resolved within the default
  90-day window; resolution time anchors that window.
- `check_autoevo_pending` surfaces unsnoozed pending entries at `/hi`.
  `/autoevo-review` retains apply / skip / defer / explain-more.
- Human apply still requires approval; skip records a dismissal reason.
  Defer updates surfacing state, snooze and any default deadline.
- Expired dismiss defaults resolve in place; successful stale banners resolve
  applied with reason `default after veto window`. Every resolution carries
  decision-ledger evidence, including idempotent detection of human reverts.
- Review auto-dismiss retains its three-skip or default 30-day maximum-age
  rule. Resolved entries remain available for dedupe and precedent.

## Quarantine

`<paths.meta>/autoevo_quarantine.toml` is owned by
`scripts/autoevo_quarantine.py`. Three consecutive no-envelope dispatches
quarantine a scope for 30 days. The threshold-crossing attempt records its
failure; filtering begins on the next plan, without double-counting it.
Success clears the streak. Expiry is evaluated against the Prefect cycle
date; a failure after expiry restarts at one. Manual reset requires an
explicitly reviewed state edit. A valid partial envelope is not a failure.

Planning keeps ordered wip, rotated eligible research subdirectory, and
reflections dispatches, with the existing per-scope caps. Quarantined or
unavailable scopes remain visible as incomplete coverage; never invent
replacement sweeps to satisfy the verifier.

## Publication and recovery

Only the trusted parent calls `autoevo_commit.publish_changes`. Each
operation records its exact intent before publication, uses explicit paths,
rechecks user-edit protection, and commits before the next operation.
Author/committer are `Atelier Autoevo Bot <noreply@atelier.local>`; never
attribute automated work or co-authorship to the user. Subjects retain
`[autoevo:<category>]`; bodies retain evidence and relevant cluster identity.

A failure after publication begins becomes `needs_review`. Do not replay
the model, retry an operation, reset the index, or automatically roll back
paths that may now contain user edits. An exact already-committed intent may
reconcile missing record evidence only; ambiguity requires effects review.
Legacy receipts are not automatically migrated or replayed. Historical
unresolved or unrecognized receipts block later cycles too; an operation
without commit proof needs review unless it records an explicit pre-write refusal.

Recovery stays user-driven: inspect the derived audit or
`git log --grep='\\[autoevo:'`, revert a chosen operation, or recover an
archived regular file from `<paths.archive>/decayed/`. No push is performed.

### Revert tombstones

Merge and stale-banner commits include `cluster_hash: <12 hex chars>`,
the first 12 hex characters of SHA-1 over sorted unique relative source
paths, one per line with a final LF. Before applying, the parent checks
matching Autoevo commits and their user reverts in the last 90 days.
A matching revert routes the cluster to pending instead of undoing the undo.

Manual `<paths.meta>/autoevo_tombstones.toml` entries keep `cluster_hash`,
`sources`, `reason`, `created_at` and optional `expires_at`; no expiry
means permanent. Automatic tombstones expire after 90 days. Both checks
remain mandatory for merges and stale-banner defaults.

## Related

- `routines/_adapters/autoevo/PROCEDURE.md`: model proposal procedure.
- `agents/forgetter.md` and `agents/curator.md`: heuristic
  and preservation contracts.
- `skills/autoevo-review/SKILL.md`: approved human triage.
- `protocols/runtime-adapters.md` and `protocols/remote-routines.md`:
  execution boundary, scheduling and operational recovery.
