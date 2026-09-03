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
uv run --quiet python3 scripts/autoevo_run.py identity
```

Bind `run_ts` → `$RUN_TS` and `run_date` → `$RUN_DATE`. An `error` is fatal
(exit 1): an unattended invocation must carry a validated `ATELIER_ROUTINE_CYCLE`.

## Step 1: Plan (gates + paths + rotation + quarantine filter)

```bash
uv run --quiet python3 scripts/autoevo_run.py plan --run-ts "$RUN_TS" --run-date "$RUN_DATE"
```

One JSON object. Bind for the rest of the run: `paths.cache` → `$PATHS_CACHE`,
`paths.findings` → `$PATHS_FINDINGS`, `paths.findings_rel` → `$FINDINGS_REL`,
`paths.audit_rel` → `$AUDIT_REL`, `outcomes_file` → `$OUTCOMES_FILE`,
`quarantine_skipped_file` → `$QUARANTINE_SKIPPED`, `protected_file` →
`$AUTOEVO_PROTECTED_FILE` and `export` it, so every commit inherits it. Keep a
register `DECAY_REPORT_RELS` of persisted reports. Never hardcode vault
segments; the plan output is the canonical resolution.

- **`gate.status: "blocked"`** — write the audit log (step 7 format) with one
  § Skipped line per blocker (`<gate>: <detail>`), then exit 0. The runner
  already ran `autoevo_preflight.py`; this repeat is defense in depth because
  Git and session state can change between the runner check and the first
  write.
- **`gate.status: "ready"`** — `dispatches` is tonight's ordered dispatch list
  (wip, one research subdir by day-of-month rotation, reflections — already
  quarantine-filtered), `quarantine_skipped` carries the `scope_quarantined:`
  lines for step 7, and `notes` carries rotation/empty-tier observations for
  audit § Notes. `protected_paths` lists in-scope files carrying uncommitted
  user edits: never propose, merge, archive, or delete one. They are refused
  at the commit choke point regardless, so proposing one only wastes a dispatch.

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
   `$PATHS_FINDINGS/decay-${RUN_TS}-<dispatch.slug>.md` and register it in
   `DECAY_REPORT_RELS` as `${FINDINGS_REL}/decay-${RUN_TS}-<slug>.md`. A report
   counts as persisted only once step 7 commits it with the audit log.
4. **Parse `findings_inline`** into rows for step 3. `mode: partial` adds the
   `forgetter_partial:` § Notes row (see condition 2).

## Step 3: Route findings by trust band

Write every parsed row to `$PATHS_CACHE/autoevo-${RUN_TS}-findings.json` as a
JSON list of `{category, confidence, candidate, peers, scores, mode,
conditions_met, claim, contradicting_peer, contradiction_signal}` (vault-
relative paths; omit fields a category lacks), then:

```bash
uv run --quiet python3 scripts/autoevo_run.py route-bands \
  --findings "$PATHS_CACHE/autoevo-${RUN_TS}-findings.json" --today "$RUN_DATE"
```

The helper owns the thresholds (`BAND_RULES`; explained in
`protocols/autoevo.md` § Trust bands) and re-verifies every auto-apply
precondition on disk, so Forgetter's `confidence` is a hint, never the
decision. Buckets: `auto_apply` (step 4, each row carries `band` and
`band_label`), `pending` (step 5, `route_reason` becomes the evidence
suffix), `probe` (contradicted: dispatch Challenger below), `invalid` (audit
§ Notes as `route_invalid: <reason>`; no op).

**Contradicted:** dispatch Challenger (synchronous) per `probe` row:

```
Agent (subagent_type=challenger) with:
  task: probe-contradiction
  wiki_claim: <claim> / contradicting_peer: <path> / contradiction_signal: <phrase>
```

`rhetorical` → one-line note in audit § "Contradicted rhetorical dismissals".
`genuine` → pending queue with category `contradicted` (wiki rewrites need
user approval; never auto-apply).

## Step 4: Auto-apply ops (commit-per-op)

Process `redundant-high` rows first (merges create survivors), then
`low-signal-high`. For each row, `SOURCES` = candidate + peers (merge) or the
candidate alone (archive):

### 4.0 Tombstone check

```bash
uv run --quiet python3 scripts/autoevo_run.py tombstone-check \
  $(for src in "${SOURCES[@]}"; do printf ' --source %q' "$src"; done) --today "$RUN_DATE"
```

If `skip: true`, route the row to the pending queue with the returned `reason`
and continue.

### 4.1 Snapshot

```bash
uv run --quiet python3 scripts/autoevo_run.py snapshot --run-ts "$RUN_TS" \
  $(for src in "${SOURCES[@]}"; do printf ' --source %q' "$src"; done)
```

All-or-nothing copies into `$PATHS_CACHE`; every op below re-verifies its
sources against these snapshots immediately before writing, so an edit that
lands mid-run refuses the op instead of being overwritten. On `error`, queue
the row and continue. `target_rel` is the oldest-mtime source (the surviving
slug that preserves inbound `[[wikilinks]]`).

### 4a. Redundant auto-merge

Dispatch Curator with the snapshot set:

```
Agent (subagent_type=curator) with:
  operation: compact / mode: auto-apply / band: redundant-high
  source_notes: [<candidate + peers>]  snapshot_paths: [<snapshot outputs>]
  target_path: <target_rel>  evidence: <row incl. scores, mode, band_label>
```

On `auto_apply_safe: false`, queue with the `refusal_reason` and continue.
Otherwise write Curator's merged body to `$PATHS_CACHE/autoevo-${RUN_TS}-merge-<slug>.md`
(never to the vault directly) and run the op:

```bash
uv run --quiet python3 scripts/autoevo_run.py merge-op --run-ts "$RUN_TS" \
  --target "<target_rel>" $(for src in "${SOURCES[@]}"; do printf ' --source %q' "$src"; done) \
  --body "$PATHS_CACHE/autoevo-${RUN_TS}-merge-<slug>.md" --scope "<dir under $OV>" \
  --target-slug "<slug>" --band "<band_label>" \
  $(for ev in "${SOURCE_EVIDENCE[@]}"; do printf ' --source-evidence %q' "$ev"; done)
```

The helper verifies snapshots, writes the survivor, stages with the sanity
check, commits through the sole committer (pinned message shape, `cluster_hash`
for tomorrow's tombstone walk), and rolls its own paths back on any failure.
`sha` → audit § "Auto-applied"; `error` → 4c.

### 4b. Low-signal auto-archive

```bash
uv run --quiet python3 scripts/autoevo_run.py archive-target --source "<source_rel>" --run-date "$RUN_DATE"
```

On `error` (target exists), queue and log § Errors. Dispatch Curator
(`operation: archive / mode: auto-apply / band: low-signal-high`,
`target_path: <target_rel>`, evidence with words + mtime); it re-greps inbound
wikilinks and returns `auto_apply_safe`. On `false`, queue. Then:

```bash
uv run --quiet python3 scripts/autoevo_run.py archive-op --run-ts "$RUN_TS" \
  --source "<source_rel>" --target "<target_rel>" --slug "<slug>" --days-inactive "<N>" \
  --evidence "words: <N>, links_in: 0, tags: 0, mtime: <YYYY-MM-DD>" --band "<band_label>"
```

`sha` → audit § "Auto-applied"; `error` → 4c.

### 4d. Veto-expired defaults

```bash
uv run --quiet python3 scripts/autoevo_pending.py veto-expired --today "$RUN_DATE" --apply-dismissals
```

`dismissed` ids resolved in place: audit § Notes `default-dismissed: <id>`.
For each `expired` entry (`stale-banner`; `peers` holds one path): run 4.0 and
4.1 on that path (skip → stays pending, log § Notes), then

```bash
uv run --quiet python3 scripts/autoevo_run.py stale-op --run-ts "$RUN_TS" --run-date "$RUN_DATE" \
  --entry-id "<id>" --source "<peer_rel>" --phrase "<dated phrase from evidence_summary>" \
  --proposed-at "<proposed_at>" --default-at "<default_at>"
```

The helper verifies the snapshot, inserts the banner once, commits, and
resolves the entry `applied` (`by = rule` in the decision ledger).
`changed: false` means the banner already existed; the entry is still
resolved. Audit § "Auto-applied": `stale-banner: <peer_rel> (<id>)`.

### 4c. Failure handling

An op that returns `error` has already rolled back its own declared paths
(`rolled_back` lists them; never the whole worktree). Do NOT retry. Write the
error and `git -C "$OV" status` to audit § Errors, queue the row with reason
`"auto-apply aborted: <error>"`, and skip the remaining auto-apply rows this
run (queue them with reason `"auto-apply phase aborted on commit failure"`).
Continue to step 5; the queue is independent of git state.

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

Record `pending-dedupe-skipped: <N>` in audit § Notes; list `invalid` ids under
§ Errors (a malformed Forgetter envelope — fix the envelope, never hand-edit
the TOML). Then let precedent set defaults without sending vault content to
any hosted model:

```bash
uv run --quiet python3 scripts/precedent.py autoevo --today "$RUN_DATE" \
  --bundle-dir "$PATHS_CACHE/autoevo-${RUN_TS}-precedent"
```

For each `<id>.prompt.txt` written, dispatch `Agent (subagent_type=precedent-judge)`
with the prompt file path and the judgment target `<id>.json` in the same
directory (one dispatch may cover the whole directory), then:

```bash
uv run --quiet python3 scripts/precedent.py autoevo --today "$RUN_DATE" \
  --judgment-dir "$PATHS_CACHE/autoevo-${RUN_TS}-precedent"
```

Audit § Notes: `precedent: <id> <verdict|gate>` per judged entry. No bundles
(nothing to judge) is a Note, not an Error.

Record any `git revert` of an earlier autoevo op as a human `undo` line, so a
default the user undid reaches `decisions.py stats` instead of being the one
outcome the precedent judge never sees. Idempotent; safe every night:

```bash
uv run --quiet python3 scripts/autoevo_run.py record-undos --today "$RUN_DATE"
```

Commit whenever the queue file actually changed, not only when `appended` is
non-empty: precedent defaults (`set-default`) and expired dismissals
(`veto-expired --apply-dismissals`) mutate the same file, and an uncommitted
mutation leaves `$OV` dirty for the next run's step 1 gate. A dedupe-only
night changes nothing and must skip the commit, which would otherwise fail
empty:

```bash
if ! git -C "$OV" diff --quiet -- _meta/autoevo_pending.toml; then
  uv run --quiet python3 scripts/autoevo_commit.py queue \
    --summary "append <N> pending findings from <RUN_DATE> sweep" \
    --detail "Categories: redundant=<n>, time-stale-A=<n>, time-stale-B=<n>, contradicted=<n>, low-signal=<n>"
fi
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

Write `DECAY_REPORT_RELS` as a JSON list to
`$PATHS_CACHE/autoevo-${RUN_TS}-reports.json`, then finalize: the helper
updates quarantine counters from `$OUTCOMES_FILE`, inserts the plan's
`scope_quarantined:` lines into the latest § Skipped, and commits the audit
log, every registered report, and the (whitelist-ignored) quarantine state in
one path-limited commit. The verifier requires every report named by the run
to exist in the same commit as the audit.

```bash
uv run --quiet python3 scripts/autoevo_run.py finalize --run-ts "$RUN_TS" --run-date "$RUN_DATE" \
  --audit-rel "$AUDIT_REL" --outcomes "$OUTCOMES_FILE" --quarantine-skipped "$QUARANTINE_SKIPPED" \
  --reports "$PATHS_CACHE/autoevo-${RUN_TS}-reports.json" --auto "<N>" --pending "<M>" --errors "<K>"
```

`quarantined` in the result is `<Q>` (threshold transitions only). Never
remove an existing `index.lock`, reset the index, or otherwise repair Git
state here. If finalize fails after a pre-flight abort, leave the audit file on
disk, print the error, exit 0 (`check_autoevo_ran` reads the file directly).
On a normal run that passed the clean-tree gate, a finalize failure is fatal.

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
