Remote Routines
===============

How scheduled remote agents (cron-style) integrate with the atelier without leaking private content into the public harness.

## Layered architecture

Three layers, each owning a different concern.

| Layer | What lives here | Provides | Boundary |
|---|---|---|---|
| **atelier** (public, portable git repo) | `scripts/cues.py`, `.claude/commands/`, `.claude/agents/`, `protocols/` | mechanism (generic, vault-agnostic) | knows the **shape** of routine outputs (config schema + ack schema), never the **content** |
| **$OV/_meta/** (user-private vault metadata) | `routine_watch.toml`, `routine_acks.json` | policy + state (which routines, what paths, what's been read) | private; never committed to atelier |
| **claude.ai cloud** (Anthropic-managed) | routine definitions (cron expression + prompt + MCP connections) | execution (the cron itself runs here, writes back to $OV via Drive MCP) | lifecycle managed via `/schedule` skill or routines UI |

The atelier never names a specific routine, output path, or domain (career, finance, health). All of that is in `$OV/_meta/routine_watch.toml`. The atelier just declares the contract.

## Contract: routine_watch.toml

User-private config at `$OV/_meta/routine_watch.toml`. Each routine declares where it writes:

```toml
[[routine]]
name = "<routine-name>"              # human label
trigger_id = "trig_<...>"            # claude.ai routine ID
cron = "<cron expr UTC + local note>"
output_dir = "<relative path under $OV>"
file_pattern = "<glob>"              # e.g. "*.md", "*-weekly.md"
label = "<short human label>"
drive_write_enforced = true          # see Policy below — set true when Drive write is wired
# needs_drive_write_update = true    # alternative: ack migration debt (legacy routine, Drive write not yet wired). Migration debt; clear within a sprint by adding Drive write to the prompt and flipping to drive_write_enforced = true.
```

Exactly one of `drive_write_enforced` or `needs_drive_write_update` MUST be `true` for the policy cue to stay silent. The two flags are mutually exclusive in intent: the first declares compliance, the second declares migration debt being tracked.

`scripts/cues.py check_routine_outputs` reads this generically. It does NOT know what any specific routine does; it only walks the declared `output_dir` looking for files matching `file_pattern` that are newer (by filename sort) than the corresponding ack in `routine_acks.json`.

## Contract: routine_acks.json

User-private state at `$OV/_meta/routine_acks.json`:

```json
{
  "<output_dir>": "<latest_acked_filename>",
  ...
}
```

After the user reads a routine output, they update the corresponding entry. The cue stops firing once `latest_acked_filename >= latest_file_in_dir.name`.

**First run.** The file need not exist initially. `scripts/cues.py` defaults a missing `routine_acks.json` to `{}` and treats every routine as unacked (the cue will list every existing output until the user reads them). When the user acks their first routine, create `$OV/_meta/routine_acks.json` with `{"<output_dir>": "<filename>"}`. Subsequent acks add or update entries.

## Policy: all routines persist to $OV

Every cron-style remote routine MUST write its canonical output to a declared path inside $OV. Cloud-only delivery (Gmail draft, email, ephemeral session output) is allowed as a **secondary** channel for notification, but the SOT lives in $OV.

Rationale:
- **Discoverability**: cues.py can surface unreviewed routine outputs at session start. Gmail-only outputs are invisible to the harness.
- **Persistence**: routine sessions are ephemeral. Without Drive write, weekly state is lost across runs.
- **Auditability**: a per-run markdown file is grep-able, linkable from notes, and survives the routine being deleted.

Routine prompts implement this by calling Google Drive MCP `create_file` with a path under `$OV/<declared output_dir>/`. If the create_file fails, the prompt MUST print the full content as routine return value so the user can paste manually.

**Conflict-resolution rule (multi-channel routines).** When a routine uses more than one output channel (any combination of Drive, email, Calendar, or future MCP backends), the Drive file is the canonical output. Every secondary channel MUST point at the Drive file (`see $OV/<path>/<file>.md`) and cap its own content at 5 lines of summary. The user reads one source of truth, not parallel summaries.

**Presentation channels (exception to the 5-line cap).** The cap exists to prevent *parallel summaries*: a second, independently-worded account of the same run that the user must reconcile against the canonical file. A channel that delivers the canonical artifact itself is not a parallel summary and is not capped. A channel qualifies as a presentation channel only when all of these hold:

- Its content is generated from the canonical artifact, not written separately. One render, two destinations.
- It adds no claim absent from the artifact.
- The artifact is still written to `$OV` first, and the run still completes on artifact attestation, so cues, ack, and audit behave exactly as for any other routine.

Delivery failure on a presentation channel is a secondary-channel failure: it is recorded on the claim and does not fail the cycle, because the source of truth was already persisted. A routine whose *only* output is the presentation channel does not qualify under any reading; the `$OV` write is what makes the channel a presentation of something rather than the thing itself.

The daily digest is the first such routine: it renders one HTML document into its declared `$OV` output directory and mails that same document. Reading it in a mail client is the point, so a 5-line pointer to a local file the user cannot open from a phone would defeat the routine while satisfying the letter of the cap.

**Enforcement.** Three cues in `scripts/cues.py`:

1. `check_routine_policy`: fires a soft cue listing routines that declare neither `drive_write_enforced = true` nor `needs_drive_write_update = true`. Surfaces non-compliance at session start.
2. `check_routine_staleness`: fires a hard cue when a routine's latest output file is older than its expected cadence + tolerance. For local routines it also catches a completed claim newer than the latest declared artifact. Newly transferred owners receive cadence-aware grace for routines that have not yet become due. Cadence is estimated from the `cron` field. Tolerance = `max(2, cadence_days)`.
3. `check_routine_hitrate`: fires a soft cue when a routine's output count over a lookback window falls below 70% of scheduled occurrences. Local denominators begin at the current owner transfer date, and output dates are counted once. Only routines with cadence <= 7 days participate; longer-cadence routines rely on staleness detection.

## Halt conditions

Routines execute on the cloud side; the harness only observes their outputs (the Drive-written file). The atelier cannot see a routine looping, OOMing, or burning quota mid-run. The harness-side cues above (`check_routine_staleness`, `check_routine_hitrate`) detect total outages and degraded hit rates *after the fact*; they cannot stop a misbehaving in-progress routine. The only effective halt signal the atelier can emit for a remote routine is a **per-routine prompt contract** the routine itself must respect.

### Per-routine prompt contract

The harness cannot enforce these declarations; they are policy, not mechanism. A routine that violates them will not be detected by the atelier. The contract is honored by the routine author at prompt-write time, not by the harness at runtime.

Every routine prompt MUST declare the following at the top of its instructions, before any data fetch or analysis step:

1. **Single-pass scope.** One pass over the source data per cron fire. No retry loop on partial fetches. If a source is unavailable, write a Drive output that names the missing input and exit; do not retry.

2. **Cost ceiling declared in plain text.** Expected token budget for one fire (typically 5K to 50K depending on scope). The plain-text declaration lets a reviewer detect overrun in the cloud session log.

3. **External-blocker behavior.** If a required MCP connection is unreachable (Drive write fails, Gmail unreachable for a source fetch), the prompt:
   - Records the failure in the routine's session output.
   - Skips the Drive write rather than retry.
   - Does NOT silently degrade to an empty Drive file. An empty file would tombstone the missed run for `check_routine_staleness` as if it succeeded.

4. **Idempotent re-fire.** If the same routine fires twice in the same UTC day (rare cron skew, manual rerun), the second fire detects the existing Drive file and either appends or refuses. It does not overwrite a successful prior output.

## Local execution layer

Some routines need local-only tools (semantic.py, git, lint.py) that remote
cloud agents cannot access. Judgment-heavy content routines run locally via
`launchd` plus headless Codex. Deterministic derived-cache maintenance may
invoke a reviewed script directly, without an LLM, while retaining the same
machine-owner gate. The default coordination shape assigns all local work to
one explicitly claimed machine; DynamoDB remains available for intentional
active-active scheduling. Claude remains supported for interactive Atelier
workflows. Unattended model-driven routines run through Codex, whose
sandbox, sanitized environment, plugin loading, and approval policy are
enforceable. A profile may declare `fallback_runtime = "claude"`: when Codex
fails before delivering (usage limit, auth, crash; never a timeout), the
runner re-executes the cycle through headless Claude Code under its own
fences (`dontAsk` permissions, vault-only edit rules, no user settings, no
MCP). `routine_audit.py` refuses the key on profiles with plugins, shell
escape, external sends, or repo commits. The claim records both runtimes.

### Architecture

| Concern | Mechanism |
|---|---|
| Scheduler | macOS `launchd` plist per routine, fires at configured time |
| Wrapper | `scripts/routine_runner.sh` handles model-driven routines; reviewed deterministic jobs use a purpose-specific wrapper such as `scripts/semantic_index_runner.sh` |
| Runtime | Headless Codex for model-driven local routines, with an optional per-profile Claude Code fallback decided by `scripts/routine_fallback.py`; deterministic derived-cache jobs run directly; interactive selection remains in `harness/runtimes.toml` plus the gitignored local preference |
| Capability profile | `harness/routine_profiles.toml` plus each private routine's `local_profile` / `cloud_profile` mapping |
| Machine ownership | Gitignored per-machine identity plus shared `$OV/_meta/routine_owner.toml`; enforced by `scripts/routine_owner.py` |
| Optional cross-machine lock | DynamoDB conditional put (`attribute_not_exists(pk)`) via `scripts/routine_lock.py` |
| Local audit trail | `$OV/_meta/routine_runs/<routine>/<cycle_id>.toml` claim files |
| Missed-run detection | `check_local_routine_missed` computes the latest due cron occurrence after the current owner transfer |

### routine_watch.toml: local routine entry

```toml
[[routine]]
name = "<routine-name>"
support = "hybrid"                    # "local-only" | "hybrid" | "cloud-only"
local_profile = "local-research"      # from harness/routine_profiles.toml
cloud_profile = "cloud-drive-research"
execution = "local"                     # "remote" (default) | "local"
cron = "<cron expr (local time)>"
output_dir = "<relative path under $OV>"
file_pattern = "<glob>"
label = "<short human label>"
# No trigger_id (local routines have no claude.ai trigger)
# No drive_write_enforced (local routines write to $OV directly)
```

### Capability and permission boundary

`support` describes where the routine can run, while `execution` selects the
active scheduler. A supported surface must name its profile and an unsupported
surface must not. Public profiles in `harness/routine_profiles.toml` declare
the sandbox, Atelier read boundary, allowed command, native live-web policy, shell-network policy, user-config policy, CLIs, plugins or cloud
connectors, hard timeout, and human-readable permissions. Private policy only
maps routine names to those generic profiles.

Ordinary profiles set `atelier_access = "read"`; Codex starts in a fresh
disposable neutral directory and adds `$OV` as a writable root. This prevents
vault-level project instructions from persisting into a later higher-capability
run while the Atelier checkout stays outside writable roots. The maintenance profile alone declares `atelier_access =
"read-write"`. Each local profile also declares `allowed_commands`, and the
runner binds the requested command to that allowlist before a cycle is claimed.
This prevents a private routine mapping from borrowing the maintenance profile
to gain repository writes.

The profile's `permissions` array is passed into the bot adapter as the strict
model-level action allowlist. Installed connectors and CLIs remain unavailable
to the procedure unless their action appears there. This is explicit prompt
enforcement, not a shell or connector ACL; profiles avoid loading optional
plugins in the public profile registry, but a user-configured plugin is not
authorized merely because it is installed or loaded.

`web_search` and `shell_network` are separate permissions. The former governs
Codex's native web-search surface. The latter governs network access from shell
commands inside the local sandbox. A research-oriented capability row can therefore use live
web search while keeping arbitrary CLIs offline. `shell_network = "enabled"`
maps to Codex's narrow `sandbox_workspace_write.network_access=true` override;
`"disabled"` passes the explicit false override. A `danger-full-access`
maintenance profile must declare `"unrestricted"`, because that sandbox cannot
honestly promise shell-network isolation.

Audit the declarations and this machine's readiness before enabling jobs:

```bash
python3 scripts/routine_audit.py audit --check-system --json
```

For a background runtime check that must not execute the real routine, run
`scripts/routine_profile_smoke.sh <routine>` through launchd. It uses the
routine's exact local sandbox, web, reasoning, and user-config envelope, but
forbids content access and mutations. Its claim proves the Codex runtime
envelope only; `connector_access = "not-exercised"` deliberately does not claim
Gmail or other connector authentication. The system audit reports those
separately under `external_permissions_unverified`; runtime readiness must not
be interpreted as approval or proof of external content access.

After explicit user authorization, `scripts/routine_permission_smoke.sh` can
exercise `gmail:read` or `readwise:create-document` through a dedicated launchd
job and the routine's exact profile. The Gmail probe reads account metadata
only. The Readwise probe idempotently upserts a pre-existing synthetic test
URL containing no user content. Successful evidence expires after 30 days;
the audit separates required, exercised, and unexercised external permissions.
The connector result is model-reported, not an independent shell attestation.

Runtime-envelope claim contract v2 also records `approval_policy = "never"`.
This is required evidence for unattended execution; loading user configuration
must not silently restore an interactive approval policy.
The helper accepts only a dedicated `com.atelier.profile-smoke.*` launchd
service, requires launchd to be its direct parent, and records that launcher.
An interactive shell run therefore cannot create new background evidence.

Cloud connector authentication is scheduler-managed, so the local audit can
validate the requested connector set but reports its authentication as
unverified. Local readiness is enforced before a cycle is claimed.

Prepare private prompts and a manifest for ChatGPT Scheduled without enabling
a second scheduler:

```bash
python3 scripts/routine_cloud_bundle.py \
  --output "$OV/cache/routine-cloud-bundles/<bundle-name>" --json
```

The helper resolves the output path and refuses targets outside `$OV`, including
the public Atelier checkout. The generated bundle is migration input, not an activation mechanism. Test the
prompt and connectors in ChatGPT web or mobile, create the Scheduled task there, verify its
first canonical Drive artifact, and only then disable the old cloud trigger or
local plist. The local owner fence does not govern cloud tasks.

The Scheduled management page is a ChatGPT web/mobile surface. It is not
currently exposed by the Codex CLI or the Codex desktop app, so bundle
generation and audit are automated locally while creation, first-run review,
and pausing the old cloud trigger remain explicit account-UI handoff steps.

The generated adapter makes the selected cloud profile's permission list an
explicit allowlist that overrides legacy procedure text. A connected optional
plugin is capability, not authorization: local shell steps and unlisted Gmail,
Readwise, or other secondary-service actions are skipped and disclosed in the
manifest's `adaptations` field.

### Coordination config

Optional `[coordination]` table in `routine_watch.toml`:

```toml
[coordination]
backend = "owner"    # "owner" (recommended) | "dynamodb" | "none"
```

`owner` is the recommended single-machine mode. Each machine has a random ID in gitignored `harness/routine_owner.local.toml`; the active ID and monotonic generation are stored in shared `$OV/_meta/routine_owner.toml`. A non-owner machine exits before preflight, stagger, or claim-file writes, even if its launchd plist remains loaded.

Claim the current machine:

```bash
uv run scripts/routine_owner.py claim
uv run scripts/routine_owner.py status
```

Transfer later from the destination machine:

```bash
uv run scripts/routine_owner.py claim --force --source-stopped
```

Before transferring, unload the old machine's routine plists and let any active
cycle finish. `--source-stopped` is an explicit operator assertion of that
precondition. The command also refuses any synchronized shared claim still
marked `running`, but Google Drive synchronization is not an atomic lock and
cannot independently prove remote quiescence. On transfer the generation
advances, and a starting runner records and rechecks it immediately before
model execution. Use DynamoDB coordination when several machines must remain
active concurrently. The shared `owner` fence cannot be downgraded with
`ATELIER_COORDINATION=none`; ownership is a scheduler safety boundary, not an
authorization system.

When `backend = "none"` (or absent), `routine_lock.py` atomically reserves the
cycle claim on the current machine but does not coordinate separate machines.
Use it only for a truly machine-local vault. A failed or uncertain claim still
requires explicit recovery before same-cycle retry. `dynamodb` is the
active-active alternative when several machines are intentionally eligible and
exactly one should win each cycle.

### DynamoDB table

Table `atelier-routine-locks`, provisioned 1 WCU / 1 RCU (always-free tier):

| Field | Type | Purpose |
|---|---|---|
| `pk` (hash key) | String | `<routine>#<cycle_id>` |
| `machine` | String | hostname of claiming machine |
| `status` | String | `running` / `recovery-in-progress` / `retry-approved` / `completed` |
| `lease_expires_at` | Number | Diagnostic lease horizon; does not permit automatic takeover |
| `ttl` | Number | Added only after completion; garbage-collects the completed marker after seven days |

Setup: `AWS_PROFILE=atelier-lock uv run scripts/routine_lock.py setup-table`

### Claim files

Written by `routine_runner.sh` to `$OV/_meta/routine_runs/<routine>/<cycle_id>.toml`:

```toml
routine = "autoevo-nightly"
cycle_id = "2026-05-26"
machine = "atelier-mbp"
contract_version = 2
profile = "local-maintenance"
profile_fingerprint = "<sha256-of-enforced-profile>"
runtime = "codex"
atelier_access = "read-write"
owner_generation = 3
claimed_at = "2026-05-26T05:01:23-07:00"
status = "completed"
completed_at = "2026-05-26T05:08:45-07:00"
duration_seconds = 445
outcome = "delivered"
output_file = "<declared-output-dir>/<fresh-artifact>.md"
```

These are gitignored; they sync across machines via Drive's filesystem sync. The cue system reads them locally.
`owner_generation` is an integer. `0` means owner fencing is not active for
the selected coordination backend; a positive value is the synchronized owner
generation checked before execution.
`scripts/routine_claim.py` exposes `validate_claim()` as the shared field
validator for claim writers, schedulers, cycle selection, and system-audit
evidence. Its `--validate-cycle` path is the calendar-date gate used before a
selected scheduled cycle enters preflight or the model environment.
Writers accept only integer generations. Read paths normalize digit-only
strings from earlier contract-v2 claims in memory; they do not rewrite the
claim. Nonnumeric strings remain invalid.
Claim status may also be `failed`, `completion-uncertain`, `deferred`, or the
operator-created `retry-approved`. `deferred` is reserved for a deterministic
preflight that produced its declared audit artifact before any model or
mutation phase began. A later trigger may reacquire that state automatically.
Owner-mode acquire atomically writes a minimal `running` reservation before
returning, so concurrent invocations on the owner cannot both pass the
same-cycle check. A failed or uncertain cycle does not become retryable merely
because launchd fires again.

### Execution flow

```
launchd fires at scheduled time
  -> routine_runner.sh <routine> <command>
     -> routine_owner.py check
        -> if another machine owns local routines: exit 0 without shared writes
        -> if owner state is missing or malformed: fail closed
     -> routine_audit.py resolve --check-system
        -> fail before a cycle claim if support, permissions, CLIs, plugins, or launchd state are invalid
     -> start caffeinate assertion for the lifetime of the runner
     -> sleep hash(hostname) % 120 (stagger)
     -> routine_lock.py acquire (atomic owner claim reservation, or DynamoDB conditional put)
        -> if held: exit 0 (skip)
        -> if error: write a machine-specific failure diagnostic and exit
     -> write claim file (status=running)
     -> recheck the owner generation immediately before execution
     -> command-specific deterministic preflight, when declared
        -> no-effect blocker: write attested audit, status=deferred, release
        -> ready: continue
     -> headless Codex executes the registered command source and returns structured JSON
     -> routine_result.py validates a fresh nonempty artifact against routine_watch.toml
     -> on success, routine_lock.py release must attest released=true
     -> update claim file (status=completed|failed|completion-uncertain)
```

### Failure modes

| Scenario | Behavior |
|---|---|
| Non-owner machine fires | Owner gate exits 0 before runtime startup or claim-file writes. |
| Deterministic no-effect preflight blocks | Write the declared audit artifact, release coordination, record `deferred`, and permit the next scheduled trigger to reacquire the cycle. |
| Ownership changes during startup | The source scheduler must be stopped before transfer; the acquire-time check and generation recheck fence a transfer already synchronized locally. |
| Two active-active machines race | With `dynamodb`, the atomic lock lets exactly one win. |
| No machine awake | `check_local_routine_missed` cue fires at next session start |
| Machine sleeps after the runner starts | The runner holds `caffeinate -i -w <pid>` until cleanup. This does not wake a machine that was already asleep at schedule time. |
| Machine crashes mid-run | The claim stays `status=running`; owner reservation or the running DynamoDB item blocks automatic retry. After six hours the missed-run cue marks it stale and points to explicit effects review and recovery. |
| Owner record missing or malformed | Fail closed before the runtime starts. |
| AWS credentials missing | In `dynamodb` mode, write a machine-specific failure diagnostic and exit. |
| DynamoDB unreachable | Write a machine-specific failure diagnostic and exit; unknown lock state fails closed. |
| Model exits successfully without a fresh declared artifact | Record `failed`, retain the cycle lock or reservation, and require explicit effects review before retry. |
| Model succeeds but release is uncertain | Record `completion-uncertain`, exit nonzero, and leave the insert-only DynamoDB lock in place for explicit operator resolution. |

### Explicit cycle recovery

Before recovery, stop or confirm the original process has exited and inspect
the routine's external effects. If those effects completed, preserve the cycle
as completed:

```bash
uv run scripts/routine_lock.py recover <routine> --cycle <id> \
  --outcome completed --confirm-effects-reviewed
```

Only when review confirms that repeating the routine is safe, approve one
same-cycle retry:

```bash
uv run scripts/routine_lock.py recover <routine> --cycle <id> \
  --outcome safe-to-retry --confirm-effects-reviewed
```

Recovery updates the synchronized local claim for both coordination backends.
`safe-to-retry` records `retry-approved`; owner acquire consumes that state by
atomically replacing it with `running`. In DynamoDB mode the helper first
fences the remote item as `recovery-in-progress`, updates the local claim, and
then publishes central `retry-approved` state. Dynamo acquire atomically
consumes that state and tells the runner it may replace a stale synchronized
claim. An interrupted recovery therefore stays closed until the same command
is resumed.

### Scheduler and vendor risks

Routine policy is scheduler-neutral, but execution is not. Local routines
depend on macOS launchd and the Codex CLI. Cloud routines depend on the selected
account scheduler, currently claude.ai or ChatGPT Scheduled, plus its connected
services. An outage on one surface does not stop routines hosted on another.

Cloud scheduler prompts are not version-controlled by Atelier. Keep the current
private prompt body at `$OV/_routine_prompts/<name>.md` after each scheduler UI
edit. The Atelier does not automate prompt-history export, scheduler creation,
or connector reauthentication.

## How the cues fire

```
SessionStart hook → uv run scripts/cues.py --hook
                       → check_routine_outputs:
                           reads $OV/_meta/routine_watch.toml
                           for each routine entry:
                               glob output_dir for file_pattern
                               compare latest filename vs acks[output_dir]
                               if newer: collect for cue message
                           sort pending outputs by oldest latest filename
                           so registry order cannot hide review debt
                       → emit cue line if any new files found
                       → check_routine_staleness:
                           for each routine entry:
                               estimate cadence from cron field
                               extract date from latest output filename
                               if age > cadence + tolerance: flag as stale
                       → emit hard cue if any routines stale/missing output
                       → check_routine_hitrate:
                           for each routine with cadence <= 7d:
                               count files in lookback window (capped to oldest file date)
                               compare actual vs expected (lookback / cadence)
                               if rate < 70%: flag as degraded
                       → emit soft cue if any routines degraded
```

When `check_routine_outputs` fires:

```
Remote cron routines 有新 output 待 review: <label1> (<filename1>); <label2> (...). 读完后 update `_meta/routine_acks.json` ({<output_dir>: <latest filename>}) 来 mute.
```

When `check_routine_staleness` fires:

```
N routine(s) with missing/stale output: <label> (<reason>). Check the active scheduler from routine_watch.toml, then inspect its local claim/diagnostic or cloud session log.
```

## Privacy boundary

Atelier-side code MUST NOT:
- Name a specific routine (`name` field is in $OV).
- Hardcode an `output_dir` value.
- Reference a domain-specific filename pattern.
- Embed trigger IDs.

Routine identity, output policy, and acknowledgement state have two private touchpoints: `routine_watch.toml` and `routine_acks.json` under `$OV/_meta/`. Prompt bodies live separately under `$OV/_routine_prompts/`. If you need to add a new routine, append its policy to the private watch file, not to Atelier source.

## Adding a new routine

1. Choose `support` and the active `execution` surface. For local execution,
   select a public local profile, archive a validated local-adapter prompt, and
   install its launchd plist on the owner machine. For cloud execution, select
   a cloud profile, generate a private bundle, then create and first-run-test
   the task in the account scheduler UI.
2. Ensure the canonical output is written under the declared `$OV` path.
   Cloud tasks require Google Drive write access on their hosting surface;
   local tasks write the synchronized filesystem directly.
3. Append the private policy to `$OV/_meta/routine_watch.toml`:
   ```toml
   [[routine]]
   name = "<short-name>"
   support = "hybrid"
   local_profile = "<public-local-profile>"
   cloud_profile = "<public-cloud-profile>"
   execution = "local"
   cron = "<expression UTC + local note>"
   output_dir = "<relative path under $OV>"
   file_pattern = "<glob>"
   label = "<human label>"
   ```
4. Run the routine audit and test the cue locally. Never leave both the local
   and cloud schedulers active during a migration.

## Migration: legacy email-only routines

If a routine pre-dates this policy (only delivers via email/Gmail draft, no Drive write step):

1. Edit its prompt (via `/schedule update` or the UI) to add a Drive write step before the email step.
2. Add `Google-Drive` to its MCP connections.
3. Add an entry in `$OV/_meta/routine_watch.toml` with `drive_write_enforced = true`.

The policy is "all NEW routines and all UPDATED routines"; existing routines should be migrated when convenient, not all at once.

## Retiring a routine

When a routine is no longer wanted:

1. Disable the active scheduler: unload the local plist, or pause/delete the task in its cloud scheduler UI.
2. Remove its `[[routine]]` block from `$OV/_meta/routine_watch.toml`. The cue stops firing.
3. Decide what to do with the existing output files in `$OV/<output_dir>/`:
   - Keep as historical archive: no action.
   - Move to `<paths.archive>/routines/<name>/`: preserves provenance, removes from active surface.
   - Delete: only if the outputs are truly disposable.
4. Drop the matching entry from `$OV/_meta/routine_acks.json` if present.

The output directory itself is left in place (rmdir manually if empty and unwanted).

## Debugging

| Symptom | Likely cause |
|---|---|
| Cue never fires | `$OV/_meta/routine_watch.toml` missing or unparseable. Run `uv run scripts/cues.py --verbose` and look at the `routine_outputs` debug line. |
| Cue fires for already-read files | `routine_acks.json` not updated. Update `{<output_dir>: <latest filename>}`. |
| Cue fires for routine that doesn't exist anymore | Remove the `[[routine]]` block from `routine_watch.toml`. |
| Routine fires but no file appears in $OV | Check `execution` and `scheduler` in the private watch row. For local runs, inspect the canonical claim plus machine-specific failure diagnostics and launchd logs. For cloud runs, inspect the hosting scheduler's session log and connector state. |
| Filename sort gives wrong "latest" | Use `YYYY-MM-DD-...` filename prefix so lexicographic sort matches chronological sort. |

## Related

- `local-first-architecture.md` — vault tier model + aggregation/detail boundary (this doc extends it with the routine layer)
- `repo-conventions.md` — atelier vs $OV separation
- `harness-assumptions.md` — track when the routine layer assumes specific MCP behaviors
- CLAUDE.md scratch-path invariant and `scripts/README.md` — script placement rules
