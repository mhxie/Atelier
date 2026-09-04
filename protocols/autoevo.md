## Purpose

Nightly autonomous quality pass over `$OV`. The primary attempt fires at 5:00
local, with hourly lightweight checks and wake/login catch-up for a missed
calendar event or deferred attempt. It sweeps the working tiers for decay using Forgetter
heuristics, auto-applies the high-confidence band, logs uncertain findings to
a pending queue, and commits every destructive operation to git so `git
revert` is the recovery path. Surfaces unresolved items at the next `/hi` via
`scripts/cues.py`.

Companion docs:
- `.claude/agents/forgetter.md` — the four decay categories + firing heuristics this protocol acts on.
- `.claude/agents/curator.md` — the agent that performs the merge/archive ops (extended with `--auto-apply` mode by this protocol).
- `protocols/repo-conventions.md` § "$OV git push policy" — the user-driven push policy this protocol partially overrides.

## Carve-out from the $OV push policy

`protocols/repo-conventions.md` declares "the atelier does not auto-commit or auto-push; both are user-driven." Autoevo is the explicit exception:

- **Auto-commit: YES.** Every op the bot performs commits to `$OV`'s default branch before the next op starts. Git history is the recovery floor; without per-op commits, `git revert` cannot undo individual bad calls.
- **Auto-push: NO.** Push remains user-driven per the existing policy. Local commits are sufficient for recovery; remote replication is a deliberate user act.

## Schedule

macOS `launchd`, daily.

- Plist: `~/Library/LaunchAgents/com.atelier.autoevo-nightly.plist`
- Primary attempt: 05:00 local.
- Recovery checks: every hour at minute 0. A completed, failed, running, or
  completion-uncertain cycle exits before capability probes or model work. A
  `deferred` claim records `retry_after_epoch`; checks before that time also
  exit cheaply, and the first check at or after it may reacquire the cycle.
- Wake from sleep: `StartCalendarInterval` delivers a missed calendar event
  when the Mac wakes.
- Login or agent reload: `RunAtLoad = true`. Before 05:00, the runner maps a
  catch-up to yesterday only when yesterday did not complete; otherwise it
  waits for today's primary event. At or after 05:00, an absent current claim
  is a missed primary attempt and runs immediately.
- Wake-from-sleep: `pmset repeat wakeorpoweron MTWRFSU 04:55:00`
- Invocation: see the plist's `ProgramArguments` block, which delegates to `scripts/routine_runner.sh`. The wrapper sources `~/.zprofile` / `~/.profile` / `~/atelier/harness/env.local.sh` for `OV`, ensures `$OV/cache` and `$OV/_meta/routine_runs/<routine>/` exist, then runs the registered command source with headless Codex. Interactive runtime preferences do not affect launchd routines. Launchd captures aggregate stdout/stderr to `/tmp/com.atelier.autoevo-nightly.out` and `.err`. Each acquired autoevo attempt also gets a claim-owned event journal under `$OV/cache/autoevo-runner-<cycle>.log.<suffix>`, so completion evidence cannot be confused with an earlier attempt in the aggregate log.
- The bot's own audit log (what the autoevo did to the vault) is separate: `$OV/agent-findings/autoevo-applied-<YYYY-MM-DD>.md`. The `/tmp/` files capture only the shell wrapper and headless runtime output, useful for debugging launchd-level failures.

### Headless Codex boundary

The runner invokes `codex exec` with an ephemeral session, web search disabled,
user config ignored, and interactive approvals disabled. It uses
`danger-full-access` because Codex `workspace-write` protects `.git/` as
read-only, while autoevo's recovery contract requires one Git commit per
destructive operation. This is a deliberate high-trust exception for this
local bot, not the default permission profile for interactive Atelier work.
The maintenance profile gives the complete sequential sweep a two-hour
wall-clock ceiling. `scripts/command_timeout.py` measures epoch time, so sleep
does not pause or extend that ceiling.

Before launching Codex, the runner rebuilds its environment from an empty base
and passes only the local path, vault routing, hook guards, the dry-run flag,
and optional Codex location or CA settings. Credentials loaded for the distributed lock and
unrelated login-profile secrets are not inherited by model-run shell commands.

The semantic boundary remains this command contract: pre-flight clean-tree and
privacy gates; bounded sweep scopes; no wiki or daily-note writes; no push; and
recoverable per-operation commits. Project hooks remain enabled. The runner
passes `ATELIER_SKIP_LOCK_TOUCH=1` so its own SessionStart and UserPromptSubmit
hooks do not refresh the session-active lock immediately before pre-flight.

Interactive Claude selection does not affect this scheduled workflow. The
wrapper always uses headless Codex so the declared sandbox, environment, and
approval contract remain mechanically consistent.

Reversible: `launchctl unload <plist>` + `pmset repeat cancel`.

### Deterministic preflight boundary

`scripts/autoevo_preflight.py` runs after the cycle claim and before Codex.
This separates invariant checks from judgment-heavy decay work:

1. Recover an unchanged checksum-owned audit left by a prior Git blocker.
2. Verify `$OV` is a Git worktree.
3. Diagnose a missing Git index or existing `index.lock` before invoking
   `git status`.
4. Inspect worktree cleanliness, zettelm cleanliness, branch divergence, Git
   LFS push state, the session lock, and the privacy gate.
5. Run one semantic query through the production `uv` environment with
   `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` to prove the cached local
   model snapshot and Lance index are usable without a download.
6. If blocked, write the canonical audit artifact, attempt a path-limited
   audit commit only when Git permits it, return an attested `noop`, and mark
   the claim `deferred` with `retry_after_epoch`.
7. If ready, start Codex. The command repeats the mutation gates before the
   sweep as defense in depth.

A `deferred` claim means the model and mutation phase never started, so the
first hourly calendar or RunAtLoad trigger at or after `retry_after_epoch` may
safely retry without an operator effects review. Earlier checks exit before
capability probes, lock acquisition, or model work. This is distinct from
`retry-approved`, which remains the manual recovery state for a failed or
uncertain cycle.

Before 05:00, catch-up selects the previous cycle. Quarantine filtering and
state updates both evaluate expiry against that selected cycle date, not the
wall date, through `scripts/autoevo_quarantine.py`. The wrapper validates that
cycle, passes it to deterministic preflight as `--run-date`, and exposes it to
the sanitized model process as `ATELIER_ROUTINE_CYCLE`. The command fails
closed if an unattended invocation omits it. Preflight independently requires
its run date and cycle to be the same real canonical calendar date before it
can construct an audit path.

The session-active blocker sets `retry_after_epoch` to the exact six-hour lock
expiry. Other deterministic blockers use a one-hour retry delay, aligned with
the calendar check, so a repaired Git index, committed user work, restored
semantic cache, or cleared privacy finding is noticed at the next hour without
starting a model while the blocker remains.

An hourly retry that sees the same blocker and detail for the same cycle reuses
the already committed blocker audit. It advances only the deferred claim's
retry time and does not append an identical section or create another Git
commit. A changed blocker or detail gets a new audit section.

If Git cannot commit the blocker audit, the helper stores its path and SHA-256
under `<paths.cache>/`. A later preflight may commit only that exact unchanged
audit. A checksum mismatch is a hard stop; the helper never absorbs a user
edit.

Before appending a new blocker section, the helper checks the target audit path
itself. If that path already has staged, unstaged, or untracked content, the
helper leaves it unchanged and defers the audit commit. This path-level guard
applies even when unrelated worktree dirtiness is the blocker.

### Completion verification

`python3 scripts/autoevo_verify.py --cycle <YYYY-MM-DD> --json` is the
authoritative clean-cycle verifier. The runner invokes it automatically after
delivery and lock release. During this check the claim is
`completion-uncertain` with `verification = "pending"`; only a passing result
is promoted atomically to `completed` with `verification = "passed"`. A failed
check remains `completion-uncertain` and requires effects review. The verifier
rejects a wrapper-level `noop`. A passing cycle must have:

- `status = "completed"`, `verification = "passed"`, and
  `outcome = "delivered"` in the canonical claim;
- at least three `envelope_returned` entries in the latest Sweep coverage
  section;
- one non-empty `agent-findings/decay-<run-id>-*.md` report per returned
  envelope, all committed in the same Git commit as the audit;
- an outcomes sidecar whose exact scope map matches that audit section;
- a lint sidecar whose counts match the audit;
- empty Skipped and Errors sections;
- a committed audit and no dirty bot-owned path afterwards: `_meta/autoevo_*.toml`
  state, or an in-scope path outside the cycle's recorded `protected_paths`;
- a claim-owned cache event journal with ordered markers proving deterministic
  preflight passed before Codex started, delivery was validated, and the lock
  was released;
- final `verified_sweeps` and `verification_commit` fields that match the
  audit and Git evidence.

A Forgetter envelope with `mode: partial` is valid bounded coverage when it
contains the required structured envelope. Record its cap reason in the
latest audit § Notes and count it as `envelope_returned`; partial mode alone
must not populate § Skipped or § Errors. Missing envelopes, unsafe skips, and
execution faults remain verifier failures.

## Pre-flight gates

The deterministic preflight defers before model launch if any gate holds. The
command repeats the gates after launch to close races between readiness and
the first mutation. Each abort surfaces as a cue at next `/hi`.

| Gate | Check | Rationale |
|---|---|---|
| Git worktree | `git rev-parse --is-inside-work-tree` succeeds | Every mutation requires a Git recovery surface. |
| Git index | resolved Git index exists | A missing index makes `git status` resemble mass deletion plus untracked recreation. Do not misclassify it as ordinary dirtiness. |
| Git index lock | resolved `index.lock` is absent | Never delete or replace a possibly live lock. |
| Session-active lock | `<paths.cache>/atelier-session-lock` exists AND mtime < 6h | User may be mid-session; avoid collision. |
| Git operation in progress | no `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `BISECT_LOG`, `rebase-merge`, or `rebase-apply` in the git dir | A bot commit would silently complete the user's merge, rebase, cherry-pick, or bisect. |
| Dirty autoevo state | no Git status entries under `_meta/autoevo_*.toml` | The queue or quarantine file is in an unknown condition. Dirty content files inside the sweep tiers do not block: they are recorded as `protected_paths` and every op refuses them, first at snapshot verification and again at the commit choke point (`--only`, explicit paths). |
| Dirty zettelm submodule | same check inside `<paths.zettelm>/` | User is mid mobile-capture digest. |
| Privacy gate | `python3 scripts/privacy_check.py --json` returns `hit_count > 0` | Hard veto; never commit a leak. |
| Semantic readiness | Offline real query exits 0 and returns JSON | Forgetter depends on semantic retrieval; fail before model launch if the cached model, environment, or Lance index is unavailable. |

Every abort attempts to write the cue-visible audit file. A pre-existing dirty
audit path is left unchanged and reported through the routine result and
runtime log. Otherwise, its Git commit is path-limited with
`git commit --only -- <audit-path>` so an already-dirty index cannot be swept
into the audit commit. The bot never deletes a stale `index.lock`, resets the
index, or stages unrelated paths. If the path-limited audit commit fails during
an abort, the file remains on disk for `check_autoevo_ran` and the runtime log
records the Git error.

When an early attempt is blocked and a later same-day retry completes cleanly,
`check_autoevo_ran` evaluates the latest attempt in the daily audit. The
earlier skip remains in history without preserving a stale warning.

## Trust bands

### Auto-apply (no human in loop, each op commits)

The numbers below are rendered from `scripts/autoevo_run.py` `BAND_RULES`;
`harness_lint.py` fails when this table and the code disagree, and no other
prose file may restate them. `route-bands` re-verifies each precondition on
disk (scores, tiers, mtimes, mode) before an op runs.

| Category | Threshold | Op |
|---|---|---|
| Redundant | 3+ peers ≥ 0.85 retrieval AND all peers + candidate in `<paths.wip>/` AND all untouched > 30d AND mode `real` | Curator merge, then `autoevo_run.py merge-op` (band `redundant-high`) |
| Low-signal | All 5 Forgetter conditions hold AND untouched > 365d | `autoevo_run.py archive-op` (`git mv` to `<paths.archive>/decayed/<YYYY-MM-DD>-<slug>.md`; never `rm`, the archive is the recovery surface) |
| Contradicted (rhetorical) | Auto-Challenger probe says "rhetorical, not a real contradiction" | No op; audit-log entry only. |

### Default after a veto window (human may veto, silence applies)

| Category | Threshold | Default op |
|---|---|---|
| Any queued category with enough precedent | `scripts/precedent.py autoevo` (nightly step 5) finds at least 3 concordant past human decisions of the same class and the judge passes its gates (`protocols/decision-ledger.md`) | `set-default` stamps the judged verdict: `dismiss` (resolved in place when the window closes) or `stale-banner` (time-stale-A with every peer under `<paths.wip>/` or `<paths.research>/`: the nightly inserts one banner under the note's title via `scripts/autoevo_run.py stale-banner`, commits per op with `scripts/autoevo_commit.py stale`, tombstone-aware, and resolves the entry `applied`). `default_at = today + 14d`. The veto is whichever action contradicts the default: skip (`dismissed`) vetoes a `stale-banner` default, apply vetoes a `dismiss` default. Skipping a `dismiss` default agrees with it and is recorded as a confirmation. Defer restarts the window. |

The proposed action on a content-stale finding ("close, redate, or verify
X") is the user's to carry out; the system's only executable default is to
mark the note stale so retrieval stops treating it as current, or to drop the
proposal when precedent says the user would. `append --rule-defaults` can
stamp the fixed stale-banner rule without a judge; it is off by default.
Entries the judge cannot back stay human-only.

### Log to pending queue (surface at /hi)

| Category | Threshold | Action |
|---|---|---|
| Redundant | 3+ working-tier peers ≥ 0.6 but below auto-band thresholds (peers under papers, preprints, wiki, profile, or daily notes never count; see `forgetter.md` § Redundant) | Append to pending queue through `scripts/autoevo_pending.py append`, which skips clusters already pending, or resolved within its `--dedupe-days` window (default 90). |
| Time-stale (era-stale, Forgetter heuristic B) | Always | Append to pending queue. Era judgments are intent-laden; never auto-act. |
| Time-stale (content-stale, Forgetter heuristic A) | Always | Append to pending queue; a default arrives only from the precedent judge. |
| Contradicted (real) | Challenger probe confirms genuine contradiction | Append to pending queue. Wiki rewrites need human approval. |
| Low-signal | 5 conditions hold AND 90-365d untouched | Append to pending queue. |

### Never auto-act

The bot refuses any op under these paths regardless of finding:

- `<paths.wiki>/` and any localized shadow wikis declared in `[paths.wiki_localized]`.
- `<paths.daily_notes>/` (user-authored per the global writing rules; the sole system write path is Scribe `daily_note` verbatim capture, never autoevo).
- Any path outside `<paths.wip>/`, `<paths.research>/`, `<paths.reflections>/`, `<paths.agent_findings>/`.

Contradicted findings against L4 wiki entries always go to the pending queue, never auto-applied.

## Per-op commit policy

One commit per destructive op. Not one commit per night.

- **Identity**: author and committer are `Atelier Autoevo Bot <noreply@atelier.local>`. Automated changes never attribute authorship, committership, or co-authorship to the user.
- **Subject**: `[autoevo:<category>] <scope>: <summary>`. The `[autoevo:...]` prefix is the grep handle (`git log --grep='\[autoevo:'`).
- **Body**: includes the Forgetter evidence verbatim so revert reviewer has full context. Cite peer paths, retrieval scores, mtime, mode (stub/real), floor threshold.

Example — redundant merge:

```
[autoevo:redundant] wip: merge 3 notes into <slug>

Source notes:
- <paths.wip>/foo.md (retrieval 0.91, mtime 2025-12-01)
- <paths.wip>/bar.md (retrieval 0.88, mtime 2026-02-14)
- <paths.wip>/baz.md (retrieval 0.86, mtime 2026-03-05)

Auto-band: redundant-high (3 peers ≥ 0.85, all > 30d cold, mode=real, floor=0.6)
Revert: git revert <sha>
```

Example — low-signal archive:

```
[autoevo:low-signal] archive: <slug> after 412 days inactive

words: 87, links_in: 0, tags: 0, mtime: 2025-04-04
Moved: <paths.wip>/<slug>.md -> <paths.archive>/decayed/2026-05-22-<slug>.md
```

## Pending queue: `$OV/_meta/autoevo_pending.toml`

Sibling to `routine_watch.toml`. Schema:

```toml
schema_version = 1

[[pending]]
id = "20260522-050143-redundant-001"   # <bot-run-ts>-<category>-<seq>
category = "redundant"                  # redundant | time-stale-A | time-stale-B | contradicted | low-signal
proposed_action = "merge into <paths.wip>/<canonical-slug>.md"
evidence_summary = "3 peers, retrieval scores 0.78/0.72/0.61, mode=real"
peers = ["<paths.wip>/a.md", "<paths.wip>/b.md", "<paths.wip>/c.md"]
proposed_at = "2026-05-22"
last_surfaced = "2026-05-22"
surface_count = 0
status = "pending"   # pending | applied | dismissed | auto-dismissed
# Stamped by `append` on default-eligible categories (see Trust bands):
# default_action = "stale-banner"
# default_at = "2026-06-05"                # proposed_at + 14d; defer pushes it
# Written by `scripts/autoevo_pending.py` on resolution (absent while pending):
# resolved_at = "2026-06-21"              # decision date; anchors the 90-day dedupe window
# dismiss_reason = "user skipped during /autoevo-review"
```

Lifecycle:

1. **Create**: `/autoevo-nightly` appends new entries for findings below the auto-band via `scripts/autoevo_pending.py append` (deterministic escaping, atomic write, and dedupe: a finding whose sorted `peers` match an entry that is pending, or was applied, dismissed, or auto-dismissed within the helper's `--dedupe-days` window (default 90), is skipped and counted in the audit § Notes).
2. **Surface**: `scripts/cues.py` `check_autoevo_pending` reads the queue; if any entries are `status = "pending"` and not snoozed, fires one cue at session start with a category breakdown.
3. **Resolve**: `/autoevo-review` walks each pending entry; user picks apply / skip / defer / explain-more.
   - Apply → dispatch Curator in approval mode; on confirm, `autoevo_pending.py resolve --status applied` and commit.
   - Skip → `autoevo_pending.py resolve --status dismissed --reason ...`; record in audit log.
   - Defer → `autoevo_pending.py defer` (bumps `surface_count` and `last_surfaced`, and pushes `default_at` out by another 14 days when the entry carries a default); reuse `cue_snooze.json` for the snooze interval. The helper is the only queue writer; nothing hand-edits the TOML.
3b. **Default**: `/autoevo-nightly` step 5 runs `scripts/precedent.py autoevo` over new pending entries; a passing verdict becomes `default_action` / `default_at`. Step 4d runs `autoevo_pending.py veto-expired --apply-dismissals`: `dismiss` defaults resolve in place, `stale-banner` defaults get their op, a per-op commit, and `applied` with reason `default after veto window`. A skip before the deadline (status `dismissed`) is the veto; the nightly never touches resolved entries. Every resolution carries a reason and lands in the decision ledger.
4. **Auto-dismiss**: after 3 skips (`surface_count >= 3`, the helper's built-in threshold) OR `--max-age-days` (default 30) from `proposed_at` without resolution, `scripts/autoevo_pending.py auto-dismiss` sets `status = "auto-dismissed"` with a `dismiss_reason`; `/autoevo-review` writes the one-line note to the audit log. Dismissed clusters stay in the file so the next sweep does not re-propose them.

## Audit log: `<paths.agent_findings>/autoevo-applied-<YYYY-MM-DD>.md`

One file per night the bot ran. Format:

```markdown
## Autoevo Run: 2026-05-22 05:00

### Auto-applied (N)
- `[autoevo:redundant] wip: merge 3 notes` — sha abc1234
- `[autoevo:low-signal] archive: <slug>` — sha def5678

### Logged to pending queue (M)
- redundant: 2 entries
- time-stale-A: 1 entry
- contradicted: 1 entry (Challenger confirmed)

### Sweep reports (3)
- agent-findings/decay-20260522-050143-wip.md
- agent-findings/decay-20260522-050143-research.md
- agent-findings/decay-20260522-050143-reflections.md

### Skipped (reason)
- Dirty $OV working tree at 04:59:58 (3 unstaged files in <paths.research>/)

### Errors
- (none)
```

If the bot bails at a pre-flight gate, the file is still written with the Skipped section populated; the cue surfaces the skip at next /hi.

## Concurrency lock: `<paths.cache>/atelier-session-lock`

Touched (`touch <file>`) by two hook paths wired at both runtime edges in
`.claude/settings.json` and `.codex/hooks.json`:

- **SessionStart hook** → runs `uv run scripts/cues.py --hook`, which touches the lock before running cue checks. Fires once per new interactive session.
- **UserPromptSubmit hook** → runs `uv run scripts/cues.py --touch-lock 2>/dev/null || true`, which refreshes the lock and exits without running any cue check (the lock path resolves via the paths registry). Fires on every user prompt so long-running sessions refresh the lock per prompt.

`/autoevo-nightly` reads the mtime; if mtime is within the last 6h, abort with reason "session-active lock fresh."

Six hours is the bound: with the UserPromptSubmit hook in place, an actively-used session refreshes the lock per prompt, so the only way to cross the 6h window is to leave a session genuinely idle for 6 hours. If the lock file is absent (fresh install, never run an interactive session), the bot interprets this as "no recent session" and proceeds — the permissive default for first-run cases.

If the lock-touch fails (cache dir unwritable, disk full), `cues.py` logs to stderr in `--verbose` mode but never breaks the hook. A persistently-failing lock leaves the 6h window operating on stale mtime; surface this by running `uv run scripts/cues.py --hook --verbose` manually to inspect.

## Recovery surfaces

| Surface | Use when |
|---|---|
| `git log --since='1 day ago' --grep='\[autoevo:'` | Skim what the bot did last night. |
| `git revert <sha>` | Undo one specific op. The bot's next run detects the revert and tombstones the cluster (see Revert tombstones below) so it does not re-merge the same notes. |
| `git revert <range>` | Undo a whole night. |
| `<paths.archive>/decayed/` | Recover a low-signal note that was auto-archived (still a regular file; `mv` back). |
| `<paths.agent_findings>/autoevo-applied-<date>.md` | At-a-glance summary without `git log`. Skipped/Errors sections also surface as a cue at next /hi (via `check_autoevo_ran` in `scripts/cues.py`). |

The archive directory is the asymmetric safety: deletions are revert-only; archive moves are revert + manual `mv` (both work). Low-signal ops use archive rather than `rm` because the recovery surface is friendlier than reading a git-revert diff to recreate the note.

## Revert tombstones

When the user runs `git revert <[autoevo:redundant] sha>`, the bot's next run would re-flag the same peer set, re-score it at the same retrieval, and re-merge within 24-72h — undoing the user's undo. The tombstone mechanism prevents this loop.

**The cluster hash.** Every redundant auto-merge and stale-banner commit body includes a `cluster_hash: <12 hex chars>` line, computed as the first 12 hex chars of `sha1` over the sorted list of source relative paths (one per line, LF-terminated). Two compact runs on the same exact source set produce the same hash; the hash is stable across re-runs and machines as long as the path strings match.

**Auto-detection at `/autoevo-nightly` step 4.0.** For each finding the bot is about to auto-apply:

1. Compute the candidate cluster's hash from its sorted source paths.
2. Walk `git log --since='90 days ago' --grep='^\[autoevo:'` for prior compact commits, extract each commit body's `cluster_hash` line.
3. For each matching hash, check whether that commit has a corresponding revert in the same window (`git log --since='90 days ago' --grep="^Revert.*<sha-prefix>"`).
4. If any match → route the finding to the pending queue with reason `"tombstoned cluster — user reverted <orig-sha> on <date>"`. Do not auto-apply.

This is fully git-native; no external state file is required for the common case. The bot inserts its own `cluster_hash:` lines into commit bodies, and `git log` is the lookup surface.

**Manual tombstones at `$OV/_meta/autoevo_tombstones.toml`.** For pre-existing reverts (before the cluster_hash convention shipped) or for user-driven "never merge these specific notes" rules, the file may also be populated by hand:

```toml
schema_version = 1

[[tombstone]]
cluster_hash = "abc123def456"   # 12 hex chars; matches the commit-body convention
sources = ["wip/foo.md", "wip/bar.md", "wip/baz.md"]
reason = "manual: these are intentionally separate per project X notes"
created_at = "2026-05-22"
expires_at = "2027-05-22"       # optional; absent = permanent
```

The auto-detection check above takes precedence; explicit tombstones are an additional skip list. Both are checked at step 4.0.

**Expiry.** Auto-detected tombstones expire after 90 days (re-evaluated on each run by re-querying git). Manual tombstones expire on their `expires_at` date if set, or persist indefinitely. Forgetter's confidence heuristics may legitimately re-fire on the same cluster after years if the underlying notes have changed; the tombstone is a brake on auto-apply, not a permanent blocklist.

## What is out of scope

- **Push.** Bot never pushes to `origin`; push remains user-driven per the `$OV` git push convention.
- **Daily notes.** Bot never reads them as autoevo targets. CLAUDE.md's writes-and-communication rules forbid system writes to daily notes (sole exception: Scribe `daily_note` verbatim capture); this protocol upholds that.
- **Wiki rewrites.** Contradicted findings against L4 are always pending-queue, never auto-applied. The Curator wiki-edit path requires human approval.
- **Re-indexing decisions.** Autoevo does not mutate the semantic index inline.
  The owner-gated `com.atelier.semantic-index` launchd job detects corpus drift
  and runs `scripts/semantic.py index --if-stale`. Query remains read-only; a
  full `--rebuild` remains a manual recovery action.

## Related

- `protocols/remote-routines.md`: shared scheduler, ownership, capability-profile, claim, and recovery contract. Autoevo uses its local Codex path because the decay stack (`scripts/semantic.py`, `scripts/lint.py`, `scripts/trust.py`, Forgetter, Curator) is local-only.
- `protocols/repo-conventions.md` § "$OV git push policy" — the policy this carve-out partially overrides.
- `protocols/local-first-architecture.md` — tier model the trust bands key off.
- `.claude/agents/forgetter.md` — heuristic source.
- `.claude/agents/curator.md` — op executor (with `--auto-apply` extension).
