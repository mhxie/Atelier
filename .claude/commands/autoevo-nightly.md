---
description: Autonomous nightly decay sweep with auto-commit; primary macOS launchd attempt at 05:00 local with hourly deferred recovery and wake catch-up.
---
# /autoevo-nightly — Autonomous decay sweep + auto-commit

Fired by macOS `launchd` at 05:00 local, with hourly lightweight checks for a
missed or deferred cycle and wake/login catch-up. Contract:
`protocols/autoevo.md`. Deterministic mechanics: `scripts/autoevo_run.py`.

Headless invocation goes through `scripts/routine_runner.sh` (always Codex, so
the unattended sandbox and approval policy are enforceable). The run
auto-applies the high-confidence Forgetter band, logs the rest to the pending
queue, and commits every destructive op to `$OV`. It never pushes. It never
touches `<paths.wiki>/`, `<paths.daily_notes>/`, or anything outside the three
working-tier sweep scopes (`<paths.wip>/`, `<paths.research>/`,
`<paths.reflections>/`). `<paths.agent_findings>/` is a write target for the
audit log only, never a sweep scope.

Interactive invocation follows the same contract — the bot does not wait for
approval. Set `DRY_RUN=1` to preview (step 8).

## Run shape

```
launchd hourly calendar check or RunAtLoad
  -> routine_runner.sh
  -> deterministic autoevo_preflight.py
     -> blocked: audit + deferred claim, no model
     -> ready: headless Codex -> orchestrator runs this file
```

Execute this command verbatim, sequentially. No parallel agent dispatches
(each commit must complete before the next op begins). On any unrecoverable
error: abort, write the partial audit log, exit. Failures surface as cues at
next /hi.

## Halt conditions

Three signals interrupt at different levels; the audit-log mapping table below
summarizes the outcome, the per-condition prose is authoritative.

**Condition 1 — external blocker (pre-flight).** Refuses to start. Triggered
by any blocker in the step 1 `plan` gate: fresh session lock (< 6h), `$OV`
not a git work tree, missing index or present index.lock, dirty tree inside an
autoevo scope, dirty `zettelm/` submodule, or `privacy_check` hits. Exit 0
with audit § Skipped naming gate + detail; the claim records the earliest safe
retry and the hourly check retries the cycle.

**Condition 2 — per-step budget exhaustion.** Demotes one dispatch. Per-scope
caps (`max_candidates`, `time_budget_s`) and Forgetter's `maxTurns: 60`
ceiling bound each sweep; a dispatch that hits any cap returns `mode: partial`.
A returned partial envelope is a bounded successful outcome, not skipped work:
record `forgetter_partial: scope=<scope>, candidates_evaluated=<n>,
reason=<budget | max_candidates | maxTurns-self-stop>` in audit § Notes.
Do not put a returned partial envelope in § Skipped or § Errors. The post-run
verifier accepts `envelope_returned` for full and partial envelopes while
rejecting genuine skips and errors. A dispatch that returns **no envelope at
all** is never a demote — envelope emission is mandatory on completion, so
absence unambiguously signals truncation or crash: that is condition 3 input.

**Condition 3 — per-scope quarantine (cross-run).** A scope with
`forgetter_no_envelope` on 3 consecutive dispatch attempts is removed from the
next run's dispatch list. State: `$OV/_meta/autoevo_quarantine.toml`
(`[[quarantine]]` rows with `scope`, `first_failed`, `consecutive_failures`,
`reason`, `expires_at`; entries auto-expire after 30 days). `plan` filters
quarantined scopes and emits their `scope_quarantined:` lines; step 7 updates
counters from this run's outcomes. The threshold-crossing run logs only the
`forgetter_no_envelope` error; the `scope_quarantined:` § Skipped entry
appears from the NEXT run's filter — no double-log. Manual reset: delete the
`[[quarantine]]` block (or the file); a successful dispatch leaves no entry, a
failure restarts the counter at 1.

| Condition | Action | Audit log section | Exit code |
|---|---|---|---|
| 1: External blocker | refuse to start | Skipped (`<gate>: <detail>`) | 0 |
| 2: Per-step budget | demote dispatch | Notes (`forgetter_partial: ...`) | run continues |
| 3: Per-scope quarantine | scope skip | Skipped (`scope_quarantined: ...`) | run continues to next scope |

Fatal errors not covered by any condition (snapshot failed, commit aborted by
hook, queue TOML corrupted) follow steps 4c, 7, and Edge cases, and exit 1.

## Step 0: Acquire run identity

```bash
RUN_TS=$(date +%Y%m%d-%H%M%S)
if [ -n "${ATELIER_ROUTINE_PROFILE:-}" ] && [ -z "${ATELIER_ROUTINE_CYCLE:-}" ]; then
  echo "abort: unattended invocation omitted ATELIER_ROUTINE_CYCLE"
  exit 1
fi
if [ -n "${ATELIER_ROUTINE_CYCLE:-}" ]; then
  RUN_DATE=$(python3 scripts/routine_claim.py autoevo-nightly \
    --validate-cycle "$ATELIER_ROUTINE_CYCLE")
else
  # Current-date fallback is only for an explicit interactive invocation.
  RUN_DATE=$(date +%Y-%m-%d)
fi
echo "autoevo-nightly run started $RUN_TS"
```

## Step 1: Plan (gates + paths + rotation + quarantine filter)

```bash
uv run --quiet python3 scripts/autoevo_run.py plan --run-ts "$RUN_TS" --run-date "$RUN_DATE"
```

One JSON object. Bind for the rest of the run: `paths.cache` → `$PATHS_CACHE`,
`paths.archive` → `$PATHS_ARCHIVE`, `paths.findings` → `$PATHS_FINDINGS`,
`paths.findings_rel` → `$FINDINGS_REL`, `paths.audit_rel` → `$AUDIT_REL`,
`outcomes_file` → `$OUTCOMES_FILE`, `quarantine_skipped_file` →
`$QUARANTINE_SKIPPED`, `protected_file` → `$AUTOEVO_PROTECTED_FILE` and
`export` it, so every `autoevo_commit.py` call inherits it. Initialize
`DECAY_REPORT_RELS=()` as the register of persisted reports. Never hardcode vault segments; the plan output is the
canonical resolution.

- **`gate.status: "blocked"`** — write the audit log (step 7 format) with one
  § Skipped line per blocker (`<gate>: <detail>`), then exit 0. Do not proceed.
  The runner already ran `autoevo_preflight.py`; this repeat is defense in
  depth because Git and session state can change between the runner check and
  the first write.
- **`gate.status: "ready"`** — `dispatches` is tonight's ordered dispatch list
  (wip, one research subdir by day-of-month rotation, reflections — already
  quarantine-filtered), `quarantine_skipped` carries the `scope_quarantined:`
  lines for step 7, and `notes` carries rotation/empty-tier observations for
  audit § Notes. `protected_paths` lists in-scope files carrying uncommitted
  user edits: never propose, merge, archive, or delete one. They are refused at
  the commit choke point regardless, so proposing one only wastes a dispatch.

## Step 2: Forgetter sweep + persist reports

Optional accelerator: `uv run scripts/decay_scan.py --redundant --scope <tier>`
precomputes the low-signal and redundant bands; pass its JSON in the dispatch
prompt so the agent verifies instead of recomputing.

Dispatch Forgetter once per entry in `dispatches`, **synchronous and
sequential** (await each Agent call before the next — commit ordering, audit
ordering, and cluster-hash determinism all depend on it):

```
Agent (subagent_type=forgetter) with:
  scope_path: <dispatch.scope>          # absolute; pre-resolved by plan
  max_candidates: <dispatch.max_candidates>
  time_budget_s: <dispatch.time_budget_s>
```

### 2a. Per-dispatch return handling

1. **Detect the envelope.** Scan the response for `---forgetter-result---` and
   `---end-result---`. If either marker is missing, record audit § Errors
   `forgetter_no_envelope: scope=<scope>, tool_calls=<n>, duration_s=<s>,
   mode=<absent | partial>` and continue to the next dispatch (no retry this
   run).
2. **Record the outcome** (both `mode: full` and `mode: partial` are
   `envelope_returned`; only a missing envelope is `forgetter_no_envelope`):

```bash
uv run --quiet python3 scripts/autoevo_run.py outcome \
  --file "$OUTCOMES_FILE" --scope "<dispatch.scope>" --result "<envelope_returned | forgetter_no_envelope>"
```

3. **Persist the inline findings** with the `Write` tool to
   `$PATHS_FINDINGS/decay-${RUN_TS}-<dispatch.slug>.md` and register it:
   `DECAY_REPORT_RELS+=("${FINDINGS_REL}/decay-${RUN_TS}-<slug>.md")`. A report
   counts as persisted only once step 7 commits it with the audit log.
4. **Parse `findings_inline`** for step 3. `mode: partial` adds the
   `forgetter_partial:` § Notes row (see condition 2).

## Step 3: Route findings by trust band

Per `protocols/autoevo.md` § Trust bands. If a row's `confidence` field is
absent, treat as `medium` and queue — never auto-apply on unspecified
confidence.

**Redundant:**

| Confidence | Threshold check | Route |
|---|---|---|
| `high` | 3+ peers ≥ 0.85 AND all in `<paths.wip>/` AND all + candidate untouched > 30d | Auto-apply (step 4) |
| `medium` or `low` | Anything else | Pending queue (step 5) |

**Low-signal:**

| Confidence | Threshold check | Route |
|---|---|---|
| `high` | All 5 conditions AND mtime > 365d ago | Auto-apply (step 4) |
| `medium` | All 5 conditions AND mtime 90-365d | Pending queue (step 5) |

**Time-stale:** always pending queue. Era judgments are intent-laden; never
auto-act.

**Contradicted:** dispatch Challenger (synchronous) per finding:

```
Agent (subagent_type=challenger) with:
  task: probe-contradiction
  wiki_claim: <claim text> / contradicting_peer: <path> / contradiction_signal: <phrase>
```

`rhetorical` → one-line note in audit § "Contradicted rhetorical dismissals".
`genuine` → pending queue with category `contradicted` (wiki rewrites need
user approval; never auto-apply).

## Step 4: Auto-apply ops (commit-per-op)

Process redundant first (merges create survivors), then low-signal. For each
finding:

### 4.0 Tombstone check

```bash
uv run --quiet python3 scripts/autoevo_run.py tombstone-check \
  $(for src in "${SOURCES[@]}"; do printf ' --source %q' "$src"; done) --today "$RUN_DATE"
```

Runs both layers of `protocols/autoevo.md` § Revert tombstones (git-log revert
detection + explicit TOML). If `skip: true`, route the finding to the pending
queue with the returned `reason` and continue.

### 4.1 Snapshot

```bash
uv run --quiet python3 scripts/autoevo_run.py snapshot --run-ts "$RUN_TS" \
  $(for src in "${SOURCES[@]}"; do printf ' --source %q' "$src"; done)
```

All-or-nothing copies of every source into `$PATHS_CACHE` (an external edit
mid-run cannot corrupt the merge; never auto-apply on a partial snapshot set).
On `error`, route the whole finding to the pending queue and continue. The
returned `target_rel` is the oldest-mtime source — the surviving slug that
preserves inbound `[[wikilinks]]`.

### 4a. Redundant auto-merge

1. Dispatch Curator with the snapshot set:

```
Agent (subagent_type=curator) with:
  operation: compact / mode: auto-apply / band: redundant-high
  source_notes: [<candidate + peers>]
  snapshot_paths: [<snapshot outputs>]
  target_path: <target_rel from snapshot>
  evidence: <Forgetter row evidence dict including confidence: high>
```

   Curator runs its scope guards and Content Preservation Checklist and
   returns `auto_apply_safe: true | false`. On `false`, queue the finding with
   the `refusal_reason` and continue.
2. **Write** the merged body to `$OV/<target_rel>` (overwrite the survivor).
3. Stage with the sanity check (never `git add -A`):

```bash
uv run --quiet python3 scripts/autoevo_run.py stage-merge --target "<target_rel>" \
  $(for src in "${SOURCES[@]}"; do printf ' --source %q' "$src"; done)
```

   On `error` (staged set diverged), run the step 4c rollback for this op's
   paths, log audit § Errors, queue the finding, continue.
4. Commit via the sole committer (never hand-write the git call — it computes
   `cluster_hash`, renders the pinned message shape, and commits `--only`):

```bash
MERGE_RESULT=$(uv run --quiet python3 scripts/autoevo_commit.py merge \
  --scope "<relative dir under $OV>" --target-slug "<target slug>" \
  --band "redundant-high (3+ peers ≥ 0.85, all > 30d cold, mode=<stub|real>, floor=<0.5|0.6>)" \
  $(for src in "${SOURCES[@]}"; do printf ' --source %q' "$src"; done) \
  $(for ev in "${SOURCE_EVIDENCE[@]}"; do printf ' --source-evidence %q' "$ev"; done) \
  --paths "<target_rel>" "${SOURCES[@]}")
COMMIT_SHA=$(printf '%s' "$MERGE_RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sha",""))')
[ -n "$COMMIT_SHA" ] || { echo "merge commit failed: $MERGE_RESULT" >&2; false; }
```

   The `cluster_hash` body line is what the next night's tombstone check
   reads; removing it breaks revert detection.
5. Append to audit § "Auto-applied" with the SHA and a one-line summary.

### 4b. Low-signal auto-archive (through Curator)

1. Snapshot (4.1) over the single source path.
2. Derive and validate the archive target:

```bash
uv run --quiet python3 scripts/autoevo_run.py archive-target \
  --source "<source_rel>" --run-date "$RUN_DATE"
```

   On `error` (target exists), queue the finding, log § Errors, continue.
3. Dispatch Curator (`operation: archive / mode: auto-apply / band:
   low-signal-high`, `target_path: <target_rel>`, evidence dict with words +
   mtime). Curator re-greps inbound wikilinks — catching a link added since
   the sweep — and returns `auto_apply_safe`. On `false`, queue.
4. Move and commit (`git mv` records a rename, and every decayed note stays
   recoverable):

```bash
git -C "$OV" mv -- "<source_rel>" "<target_rel>"
ARCHIVE_RESULT=$(uv run --quiet python3 scripts/autoevo_commit.py archive \
  --slug "<slug>" --days-inactive "<N>" \
  --evidence "words: <N>, links_in: 0, tags: 0, mtime: <YYYY-MM-DD>" \
  --source "<source_rel>" --target "<target_rel>" \
  --band "low-signal-high (all 5 Forgetter conditions + >365d cold)")
COMMIT_SHA=$(printf '%s' "$ARCHIVE_RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sha",""))')
[ -n "$COMMIT_SHA" ] || { echo "archive commit failed: $ARCHIVE_RESULT" >&2; false; }
```

   Append to audit § "Auto-applied".

### 4c. Failure handling

If any commit fails (hook error, permission, disk full, signing prompt):

1. Do NOT retry the op. A single failure aborts the entire auto-apply phase
   for this run.
2. Roll back only this op's declared paths — never the whole worktree (an
   external edit can land after preflight and a broad restore would destroy
   it):

```bash
uv run --quiet python3 scripts/autoevo_run.py rollback --run-ts "$RUN_TS" \
  --paths "<every path this op touched>" \
  $(for src in "${SOURCES[@]}"; do printf ' --source %q' "$src"; done)
```

   Restores HEAD content for pre-existing paths, deletes paths the failed op
   created, and recovers deleted sources from the step 4.1 snapshots.
3. Write `git -C "$OV" status` output verbatim to audit § Errors.
4. Continue to step 5 — the queue is independent of git state. Remaining
   auto-apply findings queue with reason
   `"auto-apply phase aborted on commit failure"`.

## Step 5: Append to pending queue

Collect every queued finding into one JSON list; the helper owns the TOML
(never emit it by hand). Fields per `protocols/autoevo.md` § Pending queue:

```json
[{"id": "<RUN_TS>-<category>-<seq>", "category": "redundant", "proposed_action": "<short imperative>",
  "evidence_summary": "<one-line evidence>", "peers": ["<relative paths under $OV/>"],
  "proposed_at": "<RUN_DATE>", "last_surfaced": "<RUN_DATE>", "surface_count": 0, "status": "pending"}]
```

```bash
PENDING_JSON="$PATHS_CACHE/autoevo-${RUN_TS}-pending.json"   # write the list here first
uv run --quiet python3 scripts/autoevo_pending.py append --entries "$PENDING_JSON" --today "$RUN_DATE"
```

The helper appends atomically and dedupes against pending + recently-resolved
entries (90d window anchored on `resolved_at`), printing
`{"appended": [...], "skipped": [...], "invalid": [...]}`. Record
`pending-dedupe-skipped: <N>` in audit § Notes; list `invalid` ids under
§ Errors (an invalid entry means a malformed Forgetter envelope — fix the
envelope, never hand-edit the TOML).

Commit only when `appended` is non-empty (a dedupe-only night leaves the file
unchanged and the commit would fail empty):

```bash
uv run --quiet python3 scripts/autoevo_commit.py queue \
  --summary "append <N> pending findings from <RUN_DATE> sweep" \
  --detail "Categories: redundant=<n>, time-stale-A=<n>, time-stale-B=<n>, contradicted=<n>, low-signal=<n>"
```

## Step 6: Run /lint and report

```bash
uv run scripts/lint.py --json > "$PATHS_CACHE/autoevo-${RUN_TS}-lint.json"
```

Append severity counts to audit § Lint. Do not auto-fix. ERROR findings that
look caused by this run's ops (parse errors in a merged note, broken @cite
from a deleted source) also go to § Errors for human review.

## Step 7: Write audit log

Path: `$OV/$AUDIT_REL` (from plan). If the file exists for `<RUN_DATE>`,
append a new `## Autoevo Run` section. **Always write this file, even when a
gate aborted the run** — § Skipped / § Errors are what surface the abort at
next /hi via `check_autoevo_ran`. On a real sweep, read `$OUTCOMES_FILE` and
render one coverage line per recorded dispatch, absolute scopes verbatim (the
verifier matches exact keys). Format:

```markdown
## Autoevo Run: <RUN_DATE> <HH:MM>

Run ID: <RUN_TS>

### Sweep coverage (<S>)
- <absolute scope>: envelope_returned

### Sweep reports (<S>)
- agent-findings/decay-<RUN_TS>-<slug>.md

### Auto-applied (<N>)
- [autoevo:redundant] <scope>: merge <N> notes into <slug> — sha <abbrev sha>
- [autoevo:low-signal] archive: <slug> — sha <abbrev sha>

### Logged to pending queue (<M>)
- <category>: <n> entries (ids: <list>)

### Contradicted rhetorical dismissals (<K>)
- <wiki claim> vs. <peer path>: Challenger judged "rhetorical"

### Lint
- ERROR: <n>, WARN: <n>, INFO: <n>

### Notes
- (none) | forgetter_partial: ... | plan notes | pending-dedupe-skipped: <N>

### Skipped (reason)
- (none) | <gate>: <detail>

### Errors
- (none) | <error description>
```

Update quarantine state from this run's outcomes, then insert the plan's
quarantine lines into the latest `### Skipped (reason)` section before
staging (the deterministic helper owns expiry pruning, counter transitions,
TOML escaping, and section placement):

```bash
QUARANTINE_COUNT_FILE="$PATHS_CACHE/autoevo-${RUN_TS}-quarantine-count.txt"
uv run --quiet python3 scripts/autoevo_quarantine.py update \
  --outcomes "$OUTCOMES_FILE" \
  --state "$OV/_meta/autoevo_quarantine.toml" \
  --count-file "$QUARANTINE_COUNT_FILE" \
  --today "$RUN_DATE"
uv run --quiet python3 scripts/autoevo_quarantine.py insert-skipped \
  --audit "$OV/$AUDIT_REL" \
  --skipped-lines "$QUARANTINE_SKIPPED"
```

`<Q>` below comes from `$QUARANTINE_COUNT_FILE`; it counts only threshold
transitions (prior_count < 3 AND new_count >= 3).

Commit the audit log, every registered decay report, and quarantine state in
one path-limited commit — the verifier requires every report named by the run
to exist in the same commit as the audit, and a plain `git commit` could
absorb unrelated staged work:

```bash
FINAL_COMMIT_PATHS=("$AUDIT_REL")
git -C "$OV" add -- "$AUDIT_REL"

if [ ${#DECAY_REPORT_RELS[@]} -gt 0 ]; then
  git -C "$OV" add -- "${DECAY_REPORT_RELS[@]}"
  FINAL_COMMIT_PATHS+=("${DECAY_REPORT_RELS[@]}")
fi

# The quarantine TOML is whitelist-ignored; the audit subcommand force-adds
# exactly that one declared bot-owned state file when present.
uv run --quiet python3 scripts/autoevo_commit.py audit \
  --run-date "$RUN_DATE" --auto "<N>" --pending "<M>" --errors "<K>" --quarantined "<Q>" \
  --paths "${FINAL_COMMIT_PATHS[@]}" \
  $( [ -f "$OV/_meta/autoevo_quarantine.toml" ] && printf -- '--force-add _meta/autoevo_quarantine.toml' )
```

Never remove an existing `index.lock`, reset the index, or otherwise repair
Git state here. If the audit commit fails after a pre-flight abort, leave the
audit file on disk, print the error, exit 0 (`check_autoevo_ran` reads the
file directly). On a normal run that passed the clean-tree gate, an audit
commit failure is fatal.

After delivery and lock release the runner calls
`scripts/autoevo_verify.py --cycle <RUN_DATE> --json`; the claim stays
`completion-uncertain` until it passes, then becomes `completed` with
`verification = "passed"`.

## Step 8: Dry-run override

With `DRY_RUN=1`: run steps 1-3 normally; for steps 4, 5, and 7 print the
proposed ops, queue entries, and audit content without executing or writing;
exit 0.

## Step 9: Exit cleanly

Return the wrapper's structured result (`delivered` normal; `noop` when a
gate aborted but wrote its audit; `failed` fatal):

```json
{
  "routine": "autoevo-nightly",
  "outcome": "delivered",
  "output_file": "agent-findings/autoevo-applied-<RUN_DATE>.md",
  "summary": "sweeps=<S>, auto=<N>, pending=<M>, dismissed=<K>, errors=<E>, lint_errors=<L>",
  "skipped_inputs": []
}
```

Exit 0 for clean runs and gate aborts (the audit log records the skip);
exit 1 for fatal step errors. The next /hi cue surfaces § Skipped / § Errors
either way.

## Edge cases

- **No findings.** Steps 4-5 are no-ops; steps 6-7 still produce a minimal
  audit log with all sections "(none)". A valid clean night.
- **`mode: partial`.** Successful bounded coverage — see condition 2.
- **No envelope.** See step 2a; log § Errors, continue, no retry this run.
- **Curator refuses an op.** Log the refusal under § Errors; continue.
- **Mid-run external edit.** If a commit's staged set includes paths the bot
  didn't touch (the stage-merge sanity check), abort the auto-apply phase and
  queue everything remaining; log § Errors.
- **Queue TOML corrupted.** `autoevo_pending.py append` refuses to overwrite,
  parks entries in a fresh `.new-N` sidecar, and exits 2. Log both under
  § Errors; the user fixes manually.

## What this command does NOT do

- Does not push to `origin` (per `protocols/repo-conventions.md` § "$OV git push policy").
- Does not edit daily notes; does not auto-apply on `<paths.wiki>/`.
- Does not run synthesis, reflection, or user-facing output beyond the audit log.
- Does not re-index the semantic store inline; the owner-gated
  `com.atelier.semantic-index` job runs `scripts/semantic.py index --if-stale`.

## Related

- `protocols/autoevo.md` — the load-bearing contract (trust bands, tombstones, queue).
- `scripts/autoevo_run.py` — deterministic mechanics (plan, tombstones, snapshots, staging, rollback).
- `scripts/autoevo_commit.py` / `scripts/autoevo_pending.py` / `scripts/autoevo_quarantine.py` — sole writers.
- `.claude/agents/forgetter.md` — decay heuristics; `.claude/agents/curator.md` — op executor.
- `.claude/commands/autoevo-review.md` — companion morning triage.
