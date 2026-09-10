# launchd — Prefect services on macOS

Atelier uses launchd only to keep two long-lived local services alive. Prefect
owns every routine schedule, timezone, run state, log, concurrency decision,
and eligible retry. Model behavior remains governed by
`protocols/remote-routines.md`; autoevo adds the domain contract in
`protocols/autoevo.md`; semantic indexing is documented in
`sources/semantic.md`.

These files are installation templates. Replacing `__ATELIER_ROOT__`, copying
a plist, and loading or unloading it are explicit operator actions. Repository
updates never change installed LaunchAgents.

## Services

| Template | Process | Schedule behavior |
|---|---|---|
| `com.atelier.prefect-server.plist` | Local Prefect API/UI on `127.0.0.1:4200` | No routine schedule; persists Prefect state under `~/Library/Application Support/Atelier/Prefect` |
| `com.atelier.prefect-routines.plist` | `scripts/routine_prefect.py serve` | Registers and serves all validated deployments; globally limited to one active run |

Both use `RunAtLoad` and `KeepAlive`. The server must be available before
the deployment runner can remain healthy; launchd will restart the runner if it
starts too early.

## Inputs and boundaries

Public deterministic declarations live in `harness/routine_jobs.toml`.
Private model routines and vault scripts stay in
`$OV/_meta/routine_watch.toml`. Public capability profiles live in
`harness/routine_profiles.toml`.

The service wrapper sources `~/.zprofile`, `~/.profile`, and then the
gitignored `harness/env.local.sh`. Put only the vault location and optional
Prefect location or port there:

```bash
export OV="/path/to/your/vault"
# export ATELIER_PREFECT_PORT="4200"
# export ATELIER_PREFECT_HOME="$HOME/Library/Application Support/Atelier/Prefect"
# Server analytics default to disabled; set the explicit override only if wanted.
# export ATELIER_PREFECT_SERVER_ANALYTICS_ENABLED="true"
```

Do not put service credentials in routine declarations or archived prompts.
The model adapter admits only declared environment keys and validates archived
prompts for literal credentials before starting Codex. The service binds to
loopback, and its bootstrap disables Prefect server analytics by default.

## Prepare and validate

Run these from the intended Atelier checkout. They do not load services or run
a routine:

```bash
uv sync --frozen
uv run --frozen python scripts/routine_prefect.py validate --json

mkdir -p "$HOME/Library/LaunchAgents"
ATELIER_ROOT="$(pwd -P)"
for NAME in prefect-server prefect-routines; do
  SOURCE="scripts/launchd/com.atelier.$NAME.plist"
  TARGET="$HOME/Library/LaunchAgents/com.atelier.$NAME.plist"
  sed "s|__ATELIER_ROOT__|$ATELIER_ROOT|g" "$SOURCE" > "$TARGET.candidate"
  plutil -lint "$TARGET.candidate"
done
```

Review the validation output before continuing. It must list only intended
model and deterministic deployments, each with an explicit cron and IANA
timezone. Also inspect the candidate plists and confirm their absolute checkout
path.

The validation command checks declarations and schedules, without contacting
the Prefect API or running a routine. Model preparation separately checks
archived prompts, required CLIs, and installed plugins. Neither check proves
OAuth readiness; complete authentication before cutover. The digest command
owns the optional CodexBar installation and quota-only permission smoke.

## Cut over from the legacy scheduler

Never load the Prefect routine service while an old routine plist or cloud
schedule for the same routine is active. Duplicate schedulers can cause
duplicate external effects, and Prefect cannot fence a scheduler it does not
own.

1. Inventory the exact loaded legacy labels and keep their installed plist
   copies for rollback. Include public labels plus every private
   `com.atelier.routine-*` and `com.atelier.vault-job.*` label.
2. Persistently disable every inventoried label, then unload it. `bootout`
   alone is temporary: an installed plist can load again at the next login.
3. Confirm those labels are both disabled and absent.
4. Move the two validated candidate plists into place.
5. Explicitly enable and bootstrap the Prefect server, confirm its API is
   healthy, then enable and bootstrap the deployment runner.
6. Confirm both services and inspect the registered schedules before leaving
   them enabled.

Example commands for steps 2 through 6, run only after adapting the legacy
label list to the machine:

```bash
DOMAIN="gui/$(id -u)"

# This public list is only a template. Repeat both commands for every exact
# com.atelier.routine-* and com.atelier.vault-job.* label found in inventory.
for LABEL in \
  com.atelier.autoevo-nightly \
  com.atelier.semantic-index \
  com.atelier.tracking-refresh
do
  launchctl disable "$DOMAIN/$LABEL"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
done

launchctl print "$DOMAIN" | rg 'com\.atelier\.(autoevo-nightly|semantic-index|tracking-refresh|routine-|vault-job\.)'
launchctl print-disabled "$DOMAIN" | rg 'com\.atelier\.(autoevo-nightly|semantic-index|tracking-refresh|routine-|vault-job\.)'

for NAME in prefect-server prefect-routines; do
  mv "$HOME/Library/LaunchAgents/com.atelier.$NAME.plist.candidate"      "$HOME/Library/LaunchAgents/com.atelier.$NAME.plist"
done

launchctl enable "$DOMAIN/com.atelier.prefect-server"
launchctl bootstrap "$DOMAIN"   "$HOME/Library/LaunchAgents/com.atelier.prefect-server.plist"
curl --fail --silent --show-error http://127.0.0.1:4200/api/health
launchctl enable "$DOMAIN/com.atelier.prefect-routines"
launchctl bootstrap "$DOMAIN"   "$HOME/Library/LaunchAgents/com.atelier.prefect-routines.plist"

launchctl print "$DOMAIN/com.atelier.prefect-server"
launchctl print "$DOMAIN/com.atelier.prefect-routines"
PREFECT_API_URL=http://127.0.0.1:4200/api   uv run --frozen prefect deployment ls
```

A `bootout` may report that a label is absent; investigate unexpected loaded
labels or missing disabled entries instead of treating a partial list as a
complete fence. Cloud schedules must be disabled in their own UI before
enabling an equivalent local deployment.

This migration does not backfill missed cycles. After cutover, use routine
freshness cues to identify absent artifacts and manually rerun a model cycle
only after reviewing possible prior effects.

## Observe

The UI is local-only at `http://127.0.0.1:4200`. Bounded command-line checks:

```bash
curl --fail --silent --show-error http://127.0.0.1:4200/api/health
PREFECT_API_URL=http://127.0.0.1:4200/api   uv run --frozen prefect flow-run ls --limit 20
PREFECT_API_URL=http://127.0.0.1:4200/api   uv run --frozen python scripts/routine_status.py
```

Aggregate service logs are machine-local:

```text
/tmp/com.atelier.prefect-server.out
/tmp/com.atelier.prefect-server.err
/tmp/com.atelier.prefect-routines.out
/tmp/com.atelier.prefect-routines.err
```

Execution status and logs belong to Prefect. A compact receipt under
`$OV/_meta/routine_receipts/<routine>/<cycle>.toml` attests only domain
delivery and output verification; it is not a second run-state ledger.

The current M3/16 GB envelope is deliberately serial: every deployment queues
collisions at one and the runner allows one active run across all deployments.
Do not raise that limit merely because a future machine has more memory;
validate model, index, and database pressure first.

## Recovery and manual runs

A failed model attempt is not retried automatically. Review its Prefect logs,
the declared output location, and any ambiguous connector effects before
proposing a manual run. Submission does not clear receipts: `pending` or
`failed` still blocks the same cycle after review, so stop and escalate the
recovery decision explicitly. Preserve that evidence; do not relabel a started
attempt `blocked` or choose another cycle to bypass the guard.

```bash
PREFECT_API_URL=http://127.0.0.1:4200/api   uv run --frozen python scripts/routine_prefect.py run <routine>   --cycle <YYYY-MM-DD>
```

Only deterministic jobs explicitly declared `retry_safe = true` may retry.
The safe preparation phase can retry because it performs no routine-domain
effects.

To restart a service without changing its installation:

```bash
DOMAIN="gui/$(id -u)"
launchctl kickstart -k "$DOMAIN/com.atelier.prefect-server"
launchctl kickstart -k "$DOMAIN/com.atelier.prefect-routines"
```

If the API is healthy but deployments are absent, inspect the routines service
error log and rerun the read-only validation command. Do not delete Prefect's
state directory as a routine repair step.

## Roll back

Rollback is also a scheduler transfer: stop Prefect's deployment runner before
reactivating any old scheduler.

```bash
DOMAIN="gui/$(id -u)"
for NAME in prefect-routines prefect-server; do
  launchctl disable "$DOMAIN/com.atelier.$NAME"
  launchctl bootout "$DOMAIN/com.atelier.$NAME" 2>/dev/null || true
done

launchctl print "$DOMAIN" | rg 'com\.atelier\.prefect-(server|routines)'
launchctl print-disabled "$DOMAIN" | rg 'com\.atelier\.prefect-(server|routines)'
```

Confirm the two Prefect labels are both disabled and absent before re-enabling
any legacy label. A saved plist is not a complete rollback:
before bootstrapping it, either restore the compatible legacy source snapshot
at every path in its `ProgramArguments` or repoint the plist to a preserved
compatible checkout. Restore that snapshot's locked Python environment as
well; the trimmed Prefect environment is not a compatible legacy runtime.
Inspect each plist and verify that every referenced executable and script
exists (and that scripts still pass their syntax checks). Only then bootstrap
the intended legacy plists, explicitly re-enabling each exact label first:

```bash
launchctl enable "$DOMAIN/<exact-legacy-label>"
launchctl bootstrap "$DOMAIN" "/path/to/compatible/<exact-legacy-label>.plist"
```

Re-enable an equivalent cloud schedule only after all local copies are
stopped. Preserve the Prefect state directory for diagnosis and history.

## Template maintenance

After editing either plist or the service wrapper:

```bash
plutil -lint scripts/launchd/com.atelier.prefect-server.plist
plutil -lint scripts/launchd/com.atelier.prefect-routines.plist
bash -n scripts/routine_prefect_service.sh
uv run --frozen python scripts/routine_prefect.py validate --json
```

No test or repository command in this guide installs, loads, unloads, starts,
or stops a real LaunchAgent.
