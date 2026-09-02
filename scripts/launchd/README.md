# launchd — macOS scheduled jobs

Atelier-managed `launchd` plists for local scheduled work. Model-driven
autoevo behavior is governed by `protocols/autoevo.md`; deterministic semantic
cache maintenance is governed by `sources/semantic.md`.

These are user-installable artifacts: copy to `~/Library/LaunchAgents/` and load with `launchctl`. Public, vault-agnostic plists live here. Private routine-specific plists may live under `$OV/_meta/launchd/`; what gets loaded into launchd is always a machine-local copy.

## Plists

| File | Schedule | Contract |
|---|---|---|
| `com.atelier.autoevo-nightly.plist` | 05:00 primary, hourly deferred recovery, wake/login catch-up | `protocols/autoevo.md` + `.claude/commands/autoevo-nightly.md` |
| `com.atelier.semantic-index.plist` | 07:30 and 19:30 local, plus load/login catch-up | Owner-gated, offline, timeout-bounded `scripts/semantic.py index --if-stale` |
| `com.atelier.tracking-refresh.plist` | 05:30 and 17:30 local, plus load/login catch-up | Owner-gated, networked, deterministic refresh of the reminder cache consumed read-only by `daily_brief.py` |
| `$OV/_meta/launchd/com.atelier.vault-job.<name>.plist` (private) | per job | Owner-gated, networked, timeout-bounded `scripts/vault_job_runner.sh <label> <vault-relative script> [args]` for a deterministic collector that lives in the vault; no model runs |

## Install

### Step 1 — declare your vault path (one-time, per machine)

The wrapper script (`scripts/routine_runner.sh`) sources `~/atelier/harness/env.local.sh`. Create the file if it does not exist:

```bash
cat > ~/atelier/harness/env.local.sh <<'EOF'
# Atelier per-user environment overrides. Gitignored. Sourced by:
#   - scripts/routine_runner.sh (invoked by launchd plists)
# Mirror whatever your shell config (~/.zshrc / ~/.zprofile) sets so
# launchd's non-interactive shell has the same view.
export OV="/path/to/your/vault"
EOF
```

If `OV` is exported from `~/.zprofile` or `~/.profile` already (login-shell
scopes), the wrappers pick it up from there. `env.local.sh` is the fallback for
users whose `OV` lives only in `.zshrc` (interactive-only). A wrapper aborts
loudly (`ERROR: OV not set ...`) if none of those sources work; the error
surfaces in that job's `/tmp/com.atelier.*.err` log.

### Step 2: claim this machine as the local-routine owner

The recommended setup has one eligible machine at a time. Claiming creates a
gitignored random identity under `harness/`, publishes it to the shared vault,
and changes `routine_watch.toml` to `coordination.backend = "owner"`:

```bash
uv run scripts/routine_owner.py claim
uv run scripts/routine_owner.py status
```

Other machines may keep their plist copies loaded. Their runners exit before
starting a model or writing a claim file. `ATELIER_COORDINATION=none` cannot
downgrade this shared fence.

To migrate all local routines later, first unload their plists on the source
machine and wait for any active cycle to finish. Then run this on the destination:

```bash
uv run scripts/routine_owner.py claim --force --source-stopped
```

`--source-stopped` explicitly asserts that the source scheduler is quiescent;
Drive sync cannot prove this atomically. The transfer also fails if any locally
synchronized shared claim is still `status = "running"`. Wait for
the active cycle to finish or resolve the stale claim before retrying. A
successful transfer advances the shared owner generation. Then install and
load the plists there. The old machine becomes ineligible as soon as its
synchronized vault sees the new owner record.

### Step 2b: optional active-active DynamoDB coordination

Use this only when several machines are intentionally eligible and exactly one
should win each cycle. Credentials must be **non-interactive**: the job runs
with the screen locked, so `boto3` reads a dedicated static-key profile from
`~/.aws/credentials`, with no Keychain prompt.

```bash
# 1. Create a scoped IAM user (one-time, from a machine with admin creds).
#    Policy: DynamoDB GetItem/PutItem/UpdateItem on atelier-routine-locks only.
cat > /tmp/atelier-lock-policy.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem"],
    "Resource": "arn:aws:dynamodb:us-west-2:*:table/atelier-routine-locks"
  }]
}
JSON
aws iam create-user --user-name atelier-routine-lock
aws iam put-user-policy --user-name atelier-routine-lock \
  --policy-name atelier-lock --policy-document file:///tmp/atelier-lock-policy.json
aws iam create-access-key --user-name atelier-routine-lock   # note the keys

# 2. Write the keys to a non-interactive profile, then lock the file down.
cat >> ~/.aws/credentials <<'INI'

[atelier-lock]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = us-west-2
INI
chmod 600 ~/.aws/credentials

# 3. Create the DynamoDB table (one-time, from any machine).
#    Use `uv run` — boto3 lives in the project venv, not system python3.
AWS_PROFILE=atelier-lock uv run scripts/routine_lock.py setup-table

# 4. Tell routine_watch.toml to use DynamoDB:
#   [coordination]
#   backend = "dynamodb"
```

The runner reads `AWS_PROFILE` (default `atelier-lock`; override via `ATELIER_LOCK_AWS_PROFILE`). The table uses provisioned mode (1 WCU / 1 RCU, always-free tier). Running locks are never taken over automatically because prior external effects may be uncertain. A diagnostic `lease_expires_at` is recorded for operators. Only a successfully completed marker receives the table's seven-day TTL.

Skip this step for the recommended single-owner setup.

### Step 3: prepare headless Codex

The shipped default uses `codex exec`. Authenticate once interactively and
review the repo's project hooks before relying on the unattended schedule:

```bash
codex login status
codex -C .
# In the TUI, open /hooks and trust the reviewed project hooks.
```

The scheduled invocation is ephemeral, starts from a narrow sanitized
environment, and runs without interactive approvals. Before claiming a cycle,
the runner resolves the routine's generic profile from
`harness/routine_profiles.toml` and verifies local readiness with
`scripts/routine_audit.py`. Ordinary routines use `workspace-write`, start in
a fresh disposable neutral directory, and add `$OV` as a writable root while
keeping the Atelier checkout read-only. This avoids persistent vault project
instructions crossing into later profiles. Only the maintenance profile grants
Atelier writes. The profile's `allowed_commands` binding is checked before the
cycle is claimed, and its permissions are passed as a strict model-level
allowlist rather than claimed as a shell or connector ACL. Research profiles enable live web only when
declared. Native web search and shell networking are distinct: ordinary
research, synthesis, and live-web digest profiles keep shell networking
disabled. Native web search does not grant arbitrary networked CLI access.
Connector profiles retain user-level Codex configuration; other
profiles ignore it. Only bounded maintenance workflows that must write git
metadata use `danger-full-access`; its shell network is explicitly recorded as
unrestricted because that sandbox does not isolate it. Every preflight probe
and model run has a hard epoch-based wall-clock timeout, so macOS sleep, a
permission prompt, or a hung provider cannot extend a one-hour budget into an
all-day process. The model-facing shell
also sets `ZDOTDIR` to `harness/routine-shell`, so it cannot load interactive
aliases, override `$OV`, or import credentials exported by `~/.zshrc`.
Once preflight succeeds, the wrapper starts `caffeinate -i -w <runner-pid>`.
This keeps the Mac awake while the stagger, model run, artifact validation,
and cleanup are active. It does not wake a Mac that was already asleep when
the schedule became due.
The runner passes both `-a never` and the explicit
`approval_policy="never"` config override. The second guard is necessary for
connector profiles that retain user configuration; otherwise a personal
approval reviewer can restore `on-request` and stall an unattended run.

Audit all registered model-driven jobs, fixed Codex availability, machine
ownership, dependencies, plugins, plist mappings, and loaded launchd state:

```bash
python3 scripts/routine_audit.py audit --check-system --json
```

The deterministic semantic job is outside `routine_watch.toml` because it
produces a machine-local derived cache, not a canonical vault artifact. Its
plist and owner/offline runner contract are covered by
`scripts/harness_smoke.py`; inspect live state with
`launchctl print gui/$(id -u)/com.atelier.semantic-index`.

The tracking refresh follows the same deterministic-job boundary even though
its derived cache lives under `$OV`: it performs fixed API and cache transforms,
never invokes a model, and has no reviewable report artifact. `daily_brief.py`
is the integration layer and never refreshes the cache itself. Stale or failed
source sections remain visible as brief warnings. Inspect live state with
`launchctl print gui/$(id -u)/com.atelier.tracking-refresh`.

Unattended model-driven routines run through Codex. Profiles that declare
`fallback_runtime = "claude"` in `harness/routine_profiles.toml` re-execute a
cycle through headless Claude Code when Codex fails without delivering; a
timeout never falls back. The claim then carries `runtime = "claude"`,
`fallback_from`, `fallback_reason`, and `primary_exit_code`, and both
transcripts are kept under `_meta/routine_logs/<routine>/` (`<cycle>.codex.log`
and `<cycle>.log`). `ATELIER_FALLBACK_CLAUDE_MODEL` pins the fallback model.
Deterministic derived-cache jobs such as semantic maintenance run their
reviewed script directly. `atelier_runtime.py use claude` and
`ATELIER_RUNTIME=claude` affect interactive launchers only.

### Step 4: install and load the plist

```bash
PLIST=com.atelier.autoevo-nightly.plist
cp "scripts/launchd/${PLIST}" "$HOME/Library/LaunchAgents/${PLIST}"
launchctl load "$HOME/Library/LaunchAgents/${PLIST}"

PLIST=com.atelier.semantic-index.plist
cp "scripts/launchd/${PLIST}" "$HOME/Library/LaunchAgents/${PLIST}"
launchctl load "$HOME/Library/LaunchAgents/${PLIST}"

PLIST=com.atelier.tracking-refresh.plist
cp "scripts/launchd/${PLIST}" "$HOME/Library/LaunchAgents/${PLIST}"
launchctl load "$HOME/Library/LaunchAgents/${PLIST}"
```

Deterministic vault jobs follow the same private-plist path. The plist calls
`scripts/vault_job_runner.sh <label> <vault-relative script> [args]`; the
wrapper sources the login profiles, refuses absolute or `..` script paths,
runs the ownership gate, holds a wake assertion, and kills the job after
`ATELIER_VAULT_JOB_TIMEOUT_SECONDS` (default 900). It logs to whatever
`StandardOutPath` the plist names, normally
`$OV/_meta/routine_logs/launchd/<label>.out`. Use it for collectors whose
output a model-driven routine then reads, so the model judges rows instead of
opening pages under a token budget.

Install private local-routine plists from the shared vault on the owner machine:

```bash
for SOURCE in "$OV"/_meta/launchd/com.atelier.routine-*.plist "$OV"/_meta/launchd/com.atelier.vault-job.*.plist; do
  PLIST=$(basename "$SOURCE")
  cp "$SOURCE" "$HOME/Library/LaunchAgents/$PLIST"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$PLIST"
done
```

Confirm it loaded:

```bash
launchctl list | grep atelier
```

The expected output: one line per loaded plist, with PID `-` (no current run) and exit code `0` (last run, or just-loaded).

## Every plist needs a way to recover a missed cycle

A plist that fires once, at one hour, on one weekday has no second chance. When
that firing lands on a sleeping machine, or the runner defers the cycle for
readiness or contention, the cycle waits for "the next trigger" -- which for a
weekly routine is a week away.

Be careful not to over-attribute to this. Measured on 2026-08-31, it explained
none of the observed loss: every scheduled cycle had a claim, so launchd was
firing on time, and the degraded hit rates came from 95 of 279 claims being
`failed`. A failed claim is refused by `schedule_decision` by design, so extra
triggers would not have retried any of them. Recovery here is worth having for
deferrals and for a machine that is genuinely off, and it is not a fix for
routines that run and fail. For those, read the transcript (below).

So a routine plist must carry at least one of:

- **`StartCalendarInterval` with no `Hour` key** -- a launchd wildcard that fires
  at minute 0 of every hour.
- **`RunAtLoad`** -- covers login and LaunchAgent reload after a missed event.

Both are cheap. The runner's schedule gate exits immediately for completed,
fenced, and not-yet-due claims, so an hourly check on an already-finished cycle
costs a process spawn and a TOML read. `com.atelier.autoevo-nightly.plist` is
the reference shape: hourly wildcard plus `RunAtLoad`, with the intended time
enforced by the runner rather than by the calendar entry.

Check the fleet:

```bash
uv run scripts/routine_audit.py health
```

The `recovery` column reads `none` for any job that cannot recover a missed
cycle. `routine_audit.py audit --check-system` reports the same set as a
warning. It is a warning and not an error because nothing is wrong until a
cycle is actually missed.

## Diagnosing a routine that runs and fails

`StandardOutPath` in every routine plist points into `/tmp`, which macOS purges.
For three months that meant a run could fail, record `error = "model-execution-
failed"` on its claim, and have its actual reason deleted within the week. The
string on its own carries no information: it is the default assigned before the
model is invoked, and it means only that the runtime exited non-zero.

The runner now keeps what matters without depending on the plist:

- **`error_detail` on the claim** -- a credential-screened tail of the
  transcript, so `routine_audit.py health` and the session cue can say what
  happened rather than that something did.
- **`$OV/_meta/routine_logs/<routine>/<cycle>.log`** -- the full screened
  transcript, written for every finished model run, success or failure, and
  pruned to the newest ten per routine. A fallback cycle keeps both: the
  failed primary as `<cycle>.codex.log` and the fallback as `<cycle>.log`.
  It sits beside `routine_runs/` rather than in `~/Library/Logs` because claims
  show several machines running these, and a transcript on the wrong machine is
  worth as little as no transcript.

Lines the credential guard flags are replaced with a marker rather than stored,
and if the guard cannot run, nothing is kept.

Start here:

```bash
uv run scripts/routine_audit.py health
```

## Wake the Mac at the scheduled time

`launchd` will not wake a sleeping Mac on its own. A missed
`StartCalendarInterval` is delivered when the machine next wakes. The
autoevo plist also uses `RunAtLoad` so login or LaunchAgent reload catches a
missed cycle. Before 05:00, the runner targets yesterday only when yesterday
did not complete; otherwise it waits for today's primary attempt. The claim
reservation prevents duplicate same-cycle work if wake, RunAtLoad, and a
calendar event arrive close together.

The calendar interval checks at minute 0 every hour. Missing `Hour` in a
`StartCalendarInterval` dictionary is a launchd wildcard. Completed, failed,
running, and uncertain claims exit before capability or model work. A
`deferred` deterministic preflight records `retry_after_epoch`; checks before
that time also exit cheaply, and the first due check can reacquire the cycle.
Session activity retries at the exact six-hour lock expiry. Other deterministic
blockers retry after one hour, so newly committed user work or repaired local
dependencies are recognized at the next calendar check. An unchanged blocker
for the same cycle reuses its committed audit, so hourly checks do not create
duplicate audit commits.

Use `pmset` to schedule a proactive wake just before the primary time:

```bash
# Wake the Mac at 04:55 every day so the 05:00 job lands on a running system.
sudo pmset repeat wakeorpoweron MTWRFSU 04:55:00
```

Verify:

```bash
pmset -g sched
```

Cancel with:

```bash
sudo pmset repeat cancel
```

## Uninstall

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.atelier.autoevo-nightly.plist"
rm "$HOME/Library/LaunchAgents/com.atelier.autoevo-nightly.plist"
launchctl unload "$HOME/Library/LaunchAgents/com.atelier.semantic-index.plist"
rm "$HOME/Library/LaunchAgents/com.atelier.semantic-index.plist"
sudo pmset repeat cancel
```

## Manual test (without waiting for 5am)

```bash
# Note: an env-var prefix does NOT propagate through `launchctl start` (the
# job runs in launchd's environment, not your shell's), so this runs WITH the
# 0-120s hostname stagger:
launchctl start com.atelier.autoevo-nightly
tail -f /tmp/com.atelier.autoevo-nightly.out /tmp/com.atelier.autoevo-nightly.err
```

Or run the Codex wrapper directly, skipping the stagger:

```bash
ATELIER_SKIP_STAGGER=1 \
  scripts/routine_runner.sh autoevo-nightly /autoevo-nightly
```

Test semantic maintenance separately. It is deterministic, owner-gated, and
offline; it skips model loading when the index is current:

```bash
scripts/semantic_index_runner.sh
uv run scripts/semantic.py status --format json
tail -f /tmp/com.atelier.semantic-index.out /tmp/com.atelier.semantic-index.err
```

An actual index update writes one `search_efficiency` JSON report to the
`.out` log. It includes scope reduction, raw coverage, chunk count,
representative query latency, deduplication, and capsule size. A fresh no-op
does not rerun the probes or emit a report.

Test reminder tracking separately. It is deterministic, owner-gated, and
networked; the cache write is atomic and source failures preserve the last
successful section:

```bash
scripts/tracking_refresh_runner.sh
uv run scripts/daily_brief.py
tail -f /tmp/com.atelier.tracking-refresh.out /tmp/com.atelier.tracking-refresh.err
```

The audit log for the run itself (what the bot did to the vault) lives at `$OV/agent-findings/autoevo-applied-<YYYY-MM-DD>.md`; the `/tmp/` files capture aggregate wrapper and Codex CLI output. Each acquired attempt also records a private event journal under `$OV/cache/` in its claim. The claim file at `$OV/_meta/routine_runs/autoevo-nightly/<date>.toml` records status, timing, journal path, and verification evidence.

Verify that a cycle performed real Forgetter work rather than only completing
a preflight `noop`:

```bash
python3 scripts/autoevo_verify.py --cycle "$(date +%Y-%m-%d)" --json
```

For autoevo, `status = "completed"` additionally requires
`verification = "passed"`. The wrapper has then proved a real Forgetter sweep,
one committed decay report per returned sweep envelope, matching audit
sidecars, a committed clean vault, ordered claim-owned event markers, and
final Git evidence. Verification runs while the claim is
`completion-uncertain` with `verification = "pending"` so interruption cannot
leave a false success. A failed verification remains
`completion-uncertain`. Other routines use the general artifact attestation:
a fresh, nonempty file matching the routine's declared `output_dir` and
`file_pattern`.

`status = "deferred"` means the deterministic autoevo preflight wrote and
validated its audit artifact before Codex or the mutation phase started. The
claim's `retry_after_epoch` is the earliest automatic retry. The first hourly
calendar or RunAtLoad check at or after that time may reacquire the cycle.
`failed` and `completion-uncertain` still require explicit effects review.

The first manual run is also the auth smoke test. If `codex exec` cannot use the cached ChatGPT login, it logs the failure to `/tmp/com.atelier.autoevo-nightly.err`. Resolve it with `codex login`, then rerun `codex login status` and the direct wrapper test.

## Debugging coordination

```bash
# Confirm this machine owns local routines:
uv run scripts/routine_owner.py status

# Check lock status for today's cycle (uv run: boto3 lives in the venv):
uv run scripts/routine_lock.py status autoevo-nightly

# Check the canonical cycle claim. status=failed means the model or runner
# failed after acquisition; status absent means no cycle was acquired:
cat "$OV/_meta/routine_runs/autoevo-nightly/$(date +%Y-%m-%d).toml"

# Preflight and lock-acquire failures are machine-specific diagnostics:
ls -lt "$OV/_meta/routine_failures/autoevo-nightly/"

# After stopping the original process and reviewing its external effects,
# preserve a cycle whose effects completed:
uv run scripts/routine_lock.py recover <routine> --cycle <id> \
  --outcome completed --confirm-effects-reviewed

# Approve one same-cycle retry only when review confirms repeating is safe:
uv run scripts/routine_lock.py recover <routine> --cycle <id> \
  --outcome safe-to-retry --confirm-effects-reviewed

# Test lock acquire/release without running the routine:
AWS_PROFILE=atelier-lock uv run scripts/routine_lock.py acquire autoevo-nightly --cycle test
AWS_PROFILE=atelier-lock uv run scripts/routine_lock.py release autoevo-nightly --cycle test
```

Owner acquire atomically reserves the claim as `running`; a normal `failed`,
`completed`, or `completion-uncertain` claim cannot be acquired again. A
deterministic `deferred` claim can be consumed automatically because no model
or mutation phase began.
`safe-to-retry` changes the synchronized claim to `retry-approved`, which is
the manual recovery state owner acquire may consume. DynamoDB recovery updates
the same local claim and keeps a central `retry-approved` fence. Dynamo acquire
atomically consumes that state, so another machine may safely execute the
approved retry even before its local Drive copy converges.

## Path assumptions

The plists delegate to `scripts/routine_runner.sh`,
`scripts/semantic_index_runner.sh`, or `scripts/tracking_refresh_runner.sh`.
They assume:

- Atelier checked out at `~/atelier/`. Edit the plist's `ProgramArguments` path if elsewhere.
- `codex` on `PATH` via `/opt/homebrew/bin`, `/usr/local/bin`, or `~/.local/bin`. The plist and wrapper populate these locations because `launchd` does not inherit an interactive shell's `PATH`.
- `uv` on `PATH` (the runner invokes `routine_lock.py` via `uv run` so boto3 resolves from the project venv).
- `caffeinate` on `PATH` on macOS. The system audit checks it before local routines are considered ready.
- `$OV` is exported from one of: `~/.zprofile`, `~/.profile`, or `~/atelier/harness/env.local.sh` (see Install step 1). The wrapper tries all three in order and aborts loudly if none work.
- If `$OV` is inside macOS `~/Library/CloudStorage`, grant Full Disk Access to the background helper executable reported by the TCC log. Homebrew Python is the first helper that reads ownership policy. The model runtime may require its own grant on first use. Use the canonical `~/Library/CloudStorage/...` path in `env.local.sh`, not a legacy `~/Google Drive` alias. A denied prompt now times out and fails before claim creation.
- `$OV/cache/` and `$OV/_meta/routine_runs/` are created on every run via `mkdir -p`, so a fresh install does not silently fail on missing directories.
- For recommended single-owner coordination: a gitignored `harness/routine_owner.local.toml` identity matching `$OV/_meta/routine_owner.toml`.
- For optional active-active coordination: an `atelier-lock` profile in `~/.aws/credentials` (see Step 2b). Without it, DynamoDB mode fails loud rather than silently skipping.

## What the schedule does NOT do

- Does not push commits to `origin`. Per `protocols/repo-conventions.md`, push remains user-driven.
- Does not touch `<paths.wiki>/`, `<paths.daily_notes>/`, or anything outside the four working tiers.
- Does not start a new session if an existing session was active within the last 6h (see `protocols/autoevo.md` § Pre-flight gates).
