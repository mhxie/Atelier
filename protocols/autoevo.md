## Purpose and authority

Autoevo is a Prefect-owned decay sweep over the working tiers. Forgetter
supplies findings; trusted policy selects eligible operations, preserves
uncertain findings in the pending queue, and writes each operation as plain
files. It produces derived decay/audit reports, not synthesis or reflection.

Autoevo writes files; Reflect commits and pushes them. Autoevo never runs a
Git write. Wiki, localized wiki shadows, daily notes, and user edits remain
protected, except the append-only wiki edit review below. The model proposes; only the trusted parent writes the live vault.

## Execution boundary

`protocols/remote-routines.md` owns Prefect scheduling, concurrency, and retry
timing. The cycle is its validated scheduled local date. A reviewed manual
run still enters through Prefect. Direct invocation of the nightly command
without a trusted plan refuses; it must not launch another runtime.

The trusted adapter performs these phases:

1. Run read-only `scripts/autoevo_preflight.py`. A blocked result goes to
   Prefect state and its cue, without model work or a live audit.
2. Retain an authoritative plan and stage its model-readable `plan.json`
   plus eligible source snapshots under `AUTOEVO_WORKSPACE`. The model
   cannot change authorization by editing its copy.
3. Run the model with live `$OV` read-only. The only authored deliverable is
   workspace `proposal.json`; policy preview may write explicit workspace
   scratch. QMD uses a staged derived database and the original cached
   models read-only. No download, index update, or inline rebuild.
4. Validate the proposal against `autoevo_verify.proposal_schema(plan)`;
   recheck readiness, state hashes, trust bands, tombstones, and Curator
   safety. HEAD may advance meanwhile; `base_head` is provenance only.
5. Write each operation as plain files, update the queue, quarantine and
   decision ledger through their deterministic owners, run lint, derive
    actionable reports, and verify the receipt against live content.

The model never writes the live queue, ledger, quarantine, reports or notes;
touches Git; or performs lint/finalize. The routine adapter owns its
judgment work, not the parent phases above. Runtime permissions and time
limits remain in `harness/routine_profiles.toml` and
`protocols/runtime-adapters.md`.

### Read-only preflight

| Gate | Requirement |
|---|---|
| Git work tree | `$OV` is a Git work tree. Git reads are plumbing that never takes `index.lock`; branch, index and dirt do not gate. |
| Session lock | No `<paths.meta>/atelier-session-lock` touch within one hour. |
| Privacy | No public-bound privacy hits. |
| Semantic readiness | QMD status reports ready with stored documents/vectors and cached models. This is not live inference or corpus-freshness proof. |

Privacy and semantic input checks run before drafting. Each write rechecks
the session lock and its sources; it does not repeat retrieval or
public-harness scans. Transient inspection failures return structured
defer evidence. A later Prefect occurrence may retry a pre-model block;
failed, pending, or uncertain post-model effects never authorize model replay.

Interactive SessionStart, UserPromptSubmit, PostToolUse, and Stop hooks refresh
the session lock, so its age measures idle time.
The scheduled adapter suppresses its own lock touch. An absent lock means no
recent session. An unreadable lock, a missing or dangling-symlink parent, or
a symlinked lock refuses the run; the touch never creates or follows either.

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

The receipt `<paths.meta>/routine_receipts/autoevo-nightly/<cycle>.json` holds
the plan, sweeps, proposal, operations and lint. Publish one
`autoevo-applied-<cycle>.md` only for applied note changes, newly queued
findings, newly armed veto-window decisions or mutation failures needing
review. Include triage evidence, proposed actions and veto deadlines.
Unchanged pending entries, empty/skipped sweeps and input/runtime failures
stay in receipts and Prefect. Reports are derived views, never machine inputs.

`autoevo_verify.py --cycle <YYYY-MM-DD> --vault <vault> --json` requires a
complete matching result, at least three returned sweeps, no coverage/errors
or newly introduced lint errors, no half-applied or malformed operation,
and a matching audit write when a review note is required. Quarantine skips
or missing sweeps cannot masquerade as a clean cycle.

## Trust bands

### Auto-apply

The numbers below render `scripts/autoevo_run.py` `BAND_RULES`;
`harness_lint.py` checks agreement. Do not repeat them in command prose.
Trusted routing re-verifies scores, tiers, mtimes, mode and content. When at
least 80% of in-scope tracked notes share one 10-minute mtime window (a clone
or checkout), age-based bands are off for the run and the plan records why.

QMD findings carry `mode: qmd`. QMD scores
are uncalibrated ordering signals. Redundant QMD findings require content
review and human approval regardless of score.

| Category | Threshold | Op |
|---|---|---|
| Redundant | 3+ peers ≥ 0.85 retrieval AND all peers + candidate in `<paths.wip>/` AND all untouched > 30d AND mode `real` | Complete safe Curator merge; trusted parent writes band `redundant-high`. |
| Low-signal | All 5 Forgetter conditions hold AND untouched > 365d | Complete safe Curator archive; parent moves the unchanged source into `<paths.archive>/decayed/`, never unbacked deletion. |
| Contradicted (rhetorical) | Complete Challenger probe says rhetorical, not a real contradiction | No op; explanatory note only. |

Curator must return complete metadata, no remaining work, the matching band,
`mode: auto-apply` and explicit `auto_apply_safe: true`. Merge sources come
from the trusted eligible snapshot; the oldest source is the survivor. Preserve
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
target. Wiki, localized wiki shadows and daily notes never auto-apply
beyond the wiki edit review.
A source is eligible only when it is tracked at HEAD, its bytes are that
blob, it has no conflict-marker lines, and its mtime is over two hours old;
the plan lists the rest as `protected_paths`. Right before each write the
parent rechecks bytes, blob and markers, and skips an operation whose source
changed. Retrieval context is not source authorization.

## Wiki edit review

Preparation stages whole primary-wiki notes with pending claims
(`protocols/wiki-schema.md`) up to `wiki_review.CAP` claims, under the same
settled-source checks. Each staged claim carries its current text and its
newest committed non-pending text from Git. Reviewer in Claim Review mode
returns `verified`, `flagged` or `inconclusive`; a claim without previous text
is never verified.

When the cycle has no coverage errors, the parent rechecks the note bytes and
appends only `@pass: reviewer | status: <verdict> | at: <cycle> | ref:
autoevo-applied-<cycle>` after the claim's last pass line. It never edits
prose or removes records. Flagged claims wait for the user, and rewrites keep
their approval gate. The report lists each written verdict, reason and
before/after excerpt for spot checks in Reflect; a later Reflect edit marks the
claim pending again. Checking prose against external sources needs network
and is out of scope.

## Pending queue and decision history

`scripts/autoevo_pending.py` owns
`<paths.meta>/autoevo_pending.toml`; never hand-edit its TOML during a run.
The parent stages state changes privately and writes them only after
rechecking the live originals' hashes.

- Deduplicate sorted peer sets already pending or resolved within the default
  90-day window; resolution time anchors that window.
- `check_autoevo_pending` surfaces unsnoozed pending entries at `/hi`.
  `/autoevo-review` retains apply / skip / defer / explain-more.
- Human apply still requires approval; skip records a dismissal reason.
  Defer updates surfacing state, snooze and any default deadline.
- Expired dismiss defaults resolve in place; successful stale banners resolve
  applied with reason `default after veto window`. Every resolution carries
  decision-ledger evidence, including idempotent detection of human restores.
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

## Writes and recovery

Each operation receipts its intent before its atomic writes: kind, candidate
id, and per path `before_blob`, `before_sha256`, `after_sha256` and
`after_blob`. Writes go to files only; Reflect commits them with ordinary
sync commits. Verification compares live content and HEAD with the receipt:
`committed`, `pending-commit`, `reverted` (all paths back at their before
bytes), `superseded` (any other later change), `skipped` (never written),
`half-applied` (stopped between writes) or `malformed` (an unrecognized
receipt or operation). Only the last two block later cycles; restoring every
path of a half-applied operation to its before or after bytes unblocks it. Do not replay the model or retry an operation. Commit-era
receipts verify the same way from their recorded content.

Rollback is plain Git: `git restore --source=<rev> -- <path>` brings back a
before blob, and an archived source also stays under
`<paths.archive>/decayed/`.

### Veto tombstones

A reverted operation is a user veto. For 90 days its cluster, the first 12
hex characters of SHA-1 over sorted unique relative source paths (one per
line with a final LF), routes to pending instead of being reapplied, and a
reverted stale banner records an `undo` decision for its queue entry. An
archived source that a sync merge revived beside its archive copy is
`superseded` and is queued for review.

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
