Remote Routines
===============

How scheduled remote agents (cron-style) integrate with the atelier without leaking private content into the public harness.

## Layered architecture

| Layer | What lives here | Provides | Boundary |
|---|---|---|---|
| **atelier** (public, portable git repo) | `scripts/cues.py`, Prefect flow/adapter code, canonical skills/agents, `routines/registry.toml`, `protocols/` | generic mechanism, adapters, public routines | knows the **shape** of private routine outputs and receipts, never private identities |
| **<paths.private_routines>/** (user-private vault source) | `registry.toml`, optional routine packages | private routine declarations and implementations | never committed to Atelier |
| **$OV/_meta/** (user-private vault state) | `routine_acks.json`, local domain receipts | output evidence and review state | receipts do not duplicate scheduler state |
| **local Prefect** | schedules, run state/history/logs, concurrency, eligible retries | execution for local files, Git, CLIs, and fixed headless runtimes | self-hosted on loopback; lifecycle is operator-managed |
| **cloud scheduler** | routine definitions, prompt, and connector bindings | execution for cloud-accessible data and Drive persistence | lifecycle managed in the selected account scheduler |

Private routine identities and output paths belong only in the vault registry.

## Contract: private routine registry

User-private config at `<paths.private_routines>/registry.toml`. It starts with
`version = 1`; each routine declares where it writes:

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

**First run.** A missing file means `{}`: all existing outputs are unacked.
Create the mapping after the first user acknowledgment; later acks update it.

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

Delivery failure on a presentation channel is a secondary-channel failure: it
is recorded in the domain delivery metadata and does not invalidate the
canonical artifact, because the source of truth was already persisted. A
routine whose *only* output is the presentation channel does not qualify under
any reading; the `$OV` write is what makes the channel a presentation of
something rather than the thing itself.

The daily digest is the first such routine: it renders one HTML document into its declared `$OV` output directory and mails that same document. Reading it in a mail client is the point, so a 5-line pointer to a local file the user cannot open from a phone would defeat the routine while satisfying the letter of the cap.

**Enforcement.** Policy and health cues in `scripts/cues.py`:

1. `check_routine_policy`: fires a soft cue listing routines that declare neither `drive_write_enforced = true` nor `needs_drive_write_update = true`. Surfaces non-compliance at session start.
2. `check_routine_staleness`: fires a hard cue when a routine's latest output
   file is older than its expected cadence + tolerance. For local routines it
   also rejects a passed receipt newer than the latest declared artifact.
   Cadence is estimated from `cron`; tolerance is
   `max(2, cadence_days)`.
3. `check_routine_hitrate`: fires a soft cue when output count over a bounded
   lookback falls below 70% of scheduled occurrences. Output dates count once.
   Only routines with cadence <= 7 days participate.
4. `check_routine_failures`: queries bounded recent Prefect model-flow state
   and surfaces failed, crashed, cancelled, or timed-out runs. If the loopback
   API is unavailable it reports that observability gap without interpreting a
   receipt as execution state.

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

Routines that need local files, Git, or local CLIs run through a self-hosted
Prefect server plus a fixed headless-runtime adapter. Prefect is the sole local
scheduler and execution-state authority. Atelier keeps only the declarations
Prefect needs, the runtime permission boundary it cannot infer, and compact
receipts that attest domain output.

### Architecture

| Concern | Mechanism |
|---|---|
| Scheduler and history | Prefect 3 server at `127.0.0.1:4200` |
| Deployment runner | `scripts/routine_prefect.py serve` |
| Schedules | `Cron` objects with an explicit IANA timezone |
| Concurrency | one queued run per deployment and one run across the Mac |
| Model boundary | `scripts/routine_adapter.py`, headless Codex or Claude |
| Public routines and model adapters | `routines/registry.toml` |
| Private declarations | `<paths.private_routines>/registry.toml` |
| Domain evidence | `$OV/_meta/routine_receipts/<routine>/<cycle>.toml` |
| Execution status and logs | Prefect flow/task state, queried through `scripts/routine_status.py` |

The receipt is not a second execution state machine. It says only that the
declared artifact was fresh, non-empty, inside `$OV`, and matched the
routine's output declaration. Failed, crashed, cancelled, queued, and running
states live only in Prefect.

### Model routine declaration

A local model row remains private:

```toml
[[routine]]
name = "<routine-name>"
support = "hybrid"                    # "local-only" | "hybrid" | "cloud-only"
execution = "local"
runner = "model"
adapter = "archived-prompt"            # or "autoevo"
profile = "local-research"             # from harness/routine_profiles.toml
rss_sources = "<private-vault-relative>.toml"  # optional
runtime_snapshot = true               # optional, default false; bounded CLI evidence
cron = "0 6 * * *"                     # a string or non-empty array
timezone = "local"                     # or an IANA name
output_dir = "<relative path under $OV>"
file_pattern = "<glob>"
label = "<short human label>"
```

The `autoevo` adapter uses the same row shape and selects its deterministic
pre/post domain wrapper explicitly. No trigger ID or Drive-write flag is needed:
the local runtime writes directly to `$OV`.

`rss_sources` requires `adapter = "archived-prompt"` and `web:live`. Its private TOML
contains `version = 1` and `[[feed]]` rows with only `id` and `url`. Preflight
validates offline; zero-retry execution collects through `routine_feeds.mjs`
before the unchanged model sandbox. URLs stay out of Prefect parameters.
`ATELIER_ROUTINE_INPUTS` names temporary JSON: untrusted feed excerpts, not
article full text. Prompts must report counters/gaps and never refetch feeds;
collector failure means unknown coverage. The helper owns network/size limits.

`runtime_snapshot` opts archived-prompt rows into temporary
`ATELIER_RUNTIME_SNAPSHOT` evidence, not scheduler state or a new ledger.
The [runtime evidence contract](../sources/runtimes/README.md) owns discovery,
failure and cleanup semantics.

`digest.context = "<safe-key>"` provides metadata-only latest background in
`context_sources`, independent of fresh windows, caps, carry, and acks. Missing
or unsafe references become `context_warnings`. The shared digest skill owns
consumption; background never implies freshness or acknowledgment.

### Deterministic jobs

Repository-owned jobs are public `[[routine]]` rows in
`routines/registry.toml`. A private deterministic routine can use a reviewed
vault script:

```toml
[[routine]]
name = "<collector-name>"
execution = "local"
runner = "process"
script = "<relative .py or .sh path under $OV>"
args = []
cron = "30 5 * * *"
timezone = "local"
timeout_seconds = 900
retry_safe = false
```

Arguments are passed without a shell. A job may declare Prefect task retries
only when `retry_safe = true`; a declaration that combines retries with an
unsafe process is rejected.

### Runtime and permission boundary

`harness/routine_profiles.toml` declares sandbox, Atelier access, web and
shell-network policy, user-config policy, timeout, reasoning effort, required
CLIs and plugins, allowed adapters, and a strict model-level permission list.
Private rows map a routine to one of those public profiles.

Unattended model work always uses Codex. Interactive runtime preferences do not
apply, and local routine profiles cannot select a primary or fallback runtime.
Before model launch the adapter:

1. validates every declaration, schedule, timezone, output path, adapter, and
   profile;
2. confirms required CLIs and installed, enabled Codex plugins;
3. validates archived prompts and rejects literal credentials;
4. creates a clean runtime environment containing only the fixed routing and
   profile values; and
5. starts `codex exec` with approvals disabled, the declared sandbox, a
   hard timeout, an ephemeral session, and the JSON result schema.

The profile permission list is prompt-enforced, not an operating-system ACL.
The sandbox and network settings are mechanical; connectors or CLIs remain
unauthorized unless their actions are also named in `permissions`.

### Retry and overlap rules

The safe preparation task retries twice because it performs no routine-domain
effects. The Codex task has zero retries. Once a model starts, its external
effects may be ambiguous, so Prefect records the failure and waits for operator
review.

A deterministic process task retries only the count declared on a
`retry_safe = true` row. Every deployment uses collision strategy
`ENQUEUE` with limit one, and the deployment runner has a global limit of
one. This is the current single-Mac resource envelope.

### Receipt contract

The parent adapter attests ordinary model artifacts and writes contract version 4:

```toml
contract_version = 4
routine = "<routine-name>"
cycle_id = "2026-09-07"
prefect_flow_run_id = "<uuid>"
profile = "<profile>"
profile_fingerprint = "<sha256>"
runtime = "codex"
started_at = "<ISO-8601 with timezone>"
completed_at = "<ISO-8601 with timezone>"
duration_seconds = 123
outcome = "delivered"
output_file = "<vault-relative artifact>"
artifact_sha256 = "<sha256 of artifact bytes>"
verification_scope = "artifact-bytes"
result_summary = "<screened bounded summary>"
skipped_inputs = []
verification = "passed"
```

The adapter writes `pending` before launch. Failure or invalid output leaves it
pending until effects review. The parent validates freshness, path, pattern,
and nonempty content, then computes the hash; model hash claims are ignored.
A valid `noop` with skipped inputs is `blocked` because work remains.

`scripts/routine_receipts.py` validates receipts. V3 stays content-unbound and
unchanged; only v4 binds bytes. Neither proves correctness or delivery.

On replay, `passed` skips only while its artifact validates;
`blocked` may proceed and other states refuse. Invalid latest evidence becomes
`needs_review`, never an older success or an automatic retry. Autoevo instead
uses its JSON result and dedicated verifier; preflight deferral emits no domain
receipt, and `protocols/autoevo.md` owns its publication evidence.

### Recovery

Inspect Prefect state and screened logs first. The canonical runbook owns
[observation](../scripts/launchd/README.md#observe) and
[recovery/manual runs](../scripts/launchd/README.md#recovery-and-manual-runs),
including service restarts and submission through the registered deployment.
Review possible external effects before repeating a model cycle; there is no
automatic model retry or backfill. Freshness and hit-rate cues surface missing
artifacts when a sleeping Mac or stopped service misses work.

### Scheduler and vendor risks

Local execution depends on macOS launchd, the local Prefect server, and Codex.
Cloud routines depend on the selected account scheduler and its connected
services. An outage on one surface does not stop routines hosted on another.

Cloud scheduler prompts are not version-controlled by Atelier. Keep the current
private prompt body at `$OV/_routine_prompts/<name>.md` after each scheduler UI
edit. The Atelier does not automate prompt-history export, scheduler creation,
or connector reauthentication.
## How the cues fire

SessionStart invokes `scripts/cues.py --hook`. `CHECKS` in that script owns
membership, order, and exact messages; the contracts above own routine policy
and review acknowledgements.

## Privacy boundary

Public code must not embed private routine identities, output paths, domain
filename patterns, or trigger IDs. Private declarations belong in
`<paths.private_routines>/registry.toml`; public declarations belong in
`routines/registry.toml`. Acknowledgements and local domain receipts stay under
`$OV/_meta/`; private prompt bodies live under `$OV/_routine_prompts/`.

## Adding a new routine

1. Choose `support` and the active `execution` surface. For local execution,
   select a public local profile and archive a validated local-adapter prompt.
   For cloud execution, select
   a cloud profile, then create and first-run-test the task in the account
   scheduler UI.
2. Ensure the canonical output is written under the declared `$OV` path.
   Cloud tasks require Google Drive write access on their hosting surface;
   local tasks write the synchronized filesystem directly.
3. Append the private policy to `<paths.private_routines>/registry.toml`:
   ```toml
   [[routine]]
   name = "<short-name>"
   support = "hybrid"
   runner = "model"
   adapter = "archived-prompt"
   profile = "<public-local-profile>"
   cloud_profile = "<public-cloud-profile>"
   execution = "local"
   cron = "<cron expression>"
   timezone = "<IANA name or local>"
   output_dir = "<relative path under $OV>"
   file_pattern = "<glob>"
   label = "<human label>"
   ```
4. Run `uv run --frozen python scripts/routine_prefect.py validate --json` and
   test the cue locally. During activation, stop the source scheduler before
   restarting the Prefect routines service. Never leave equivalent local and
   cloud schedules active together.

## Migration: legacy email-only routines

For legacy email-only routines, add canonical Drive persistence before delivery,
bind the Google-Drive connector, and set `drive_write_enforced = true` in the
private registry. New and updated routines must comply; migrate others incrementally.

## Retiring a routine

When a routine is no longer wanted:

1. Disable the active scheduler: pause/delete the Prefect deployment, or
   pause/delete the task in its cloud scheduler UI.
2. Remove its `[[routine]]` block from `<paths.private_routines>/registry.toml`. The cue stops firing.
3. Decide what to do with the existing output files in `$OV/<output_dir>/`:
   - Keep as historical archive: no action.
   - Move to `<paths.archive>/routines/<name>/`: preserves provenance, removes from active surface.
   - Delete: only if the outputs are truly disposable.
4. Drop the matching entry from `$OV/_meta/routine_acks.json` if present.

The output directory itself is left in place (rmdir manually if empty and unwanted).

## Debugging

| Symptom | Likely cause |
|---|---|
| Cue never fires | The private routine registry is missing or unparseable. Run `uv run scripts/cues.py --verbose` and look at the `routine_outputs` debug line. |
| Cue fires for already-read files | `routine_acks.json` not updated. Update `{<output_dir>: <latest filename>}`. |
| Cue fires for routine that doesn't exist anymore | Remove the `[[routine]]` block from the private registry. |
| Routine fires but no file appears in $OV | Check `execution` in the private row. For local runs, inspect Prefect flow state/logs and any domain receipt. For cloud runs, inspect the hosting scheduler's session log and connector state. |
| Filename sort gives wrong "latest" | Use `YYYY-MM-DD-...` filename prefix so lexicographic sort matches chronological sort. |

## Related

- `local-first-architecture.md` — vault tier model + aggregation/detail boundary (this doc extends it with the routine layer)
- `repo-conventions.md` — atelier vs $OV separation
- AGENTS.md scratch-path invariant and `scripts/README.md` — script placement rules
