## Native observability

Owner: `scripts/observability/`; registry binding: `harness/runtimes.toml`.
This is measurement, not a business ledger or a billing statement. Receipts,
domain verification, Prefect run state, and model retry policy remain unchanged.

| Source | Measurement authority | Persistence |
|---|---|---|
| Scheduled Codex, including isolated drafting | Native `exec --json` turn usage | One `atelier.observation` record in the actual Prefect run log |
| Opt-in Claude CLI | Native OTel API-request events | Screened local Collector files |
| Codex interactive CLI | Wire-schema verification pending; launch opt-in refused | No claimed usage coverage |
| Native lifecycle hooks | Session/turn/agent boundaries, not tokens or cost | Same screened files, only with launcher opt-in |

### Read status

```sh
uv run --frozen python scripts/routine_status.py --usage --json
python3 scripts/atelier_runtime.py status --observations
```

Scheduled observations belong to an attempt's Prefect flow ID, not the cycle
receipt. A reused receipt does not create another usage record. The bounded
status query reads at most 200 logs for the selected runs; a missing record is
unknown, including older runs, skipped attempts, missing final logs and page
truncation. It does not backfill from transcripts. Generic process-job logging
is unchanged; raw model stdout/stderr is not mirrored to Prefect.

The JSONL reader drains malformed and oversized events without keeping their
contents. It accepts a terminal usage event once per observed turn start and
bounds item-ID deduplication. `measured_turns` describes each token field's
coverage. Missing fields stay null. Cached input is a subset of Codex input;
reasoning output is a subset of output. Do not add either twice. Model duration
does not include preparation, queues or artifact verification.

Interactive summaries are partial observations, never proof of lossless
delivery. They deduplicate request/tool identities where available, otherwise
native sequence and timestamp. They keep Claude input, output, cache-read and
cache-creation buckets separate; `measured_requests` states coverage. Summed
request duration is not session wall time. API errors and retry exhaustion are
different counters. Cost is the runtime's estimate, not invoiced cost. Subagent
summary footprints are not added to request usage. Direct chat-completion calls
and Desktop-host sessions are outside verified coverage.

### Install the local Collector

The pinned Contrib Collector is `0.160.0`. The installer verifies the published
Apple Silicon archive checksum before extracting one binary. It refuses a
different existing binary or launch agent. Download the matching archive from
the [publisher release](https://github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.160.0), then:

```sh
python3 scripts/observability/native.py install --archive /path/to/otelcol-contrib_0.160.0_darwin_arm64.tar.gz
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.atelier.observability.plist"
```

The service generates its configuration in memory from `collector.py`.
It binds OTLP HTTP to `127.0.0.1:14318` and health to `127.0.0.1:14319`.
There is no remote exporter, metrics listener, raw spool, or debug-log sink.
The binary and telemetry are under the host-local
`~/Library/Application Support/Atelier/Observability/`, outside the synced vault.
Files rotate at 8 MiB with three backups; rotated backups have a 14-day limit.
The directory is mode 0700 and the service uses umask 077. Status reads at most
four files and 32 MiB. Restarts preserve existing observations.

```sh
python3 scripts/atelier_runtime.py shell --runtime claude --observe
python3 scripts/atelier_runtime.py run --runtime claude --observe hi
```

`--observe` changes only that launched process. It supplies native exporter
settings and an invocation marker without writing global native configuration.
Project hooks remain inert without that marker. They send no prompts, tool
bodies or decisions and swallow receiver failures. Trust/managed policy may
prevent hook or exporter activation; verify delivery in a normal fresh session.
The launcher does not bypass those policies. Existing user telemetry and native
transcript behavior outside this observation path are not a coverage claim.

Native content flags alone are insufficient: resource metadata can include
identity and paths. Before export the Collector constructs a fresh body from
fixed event names, numeric fields, recognized model names and hashed identifiers.
Its body-only JSON encoder excludes all original body/resource/scope fields,
including schema URLs. Unknown sources and processing errors drop before disk.
The encoder and file exporter are alpha, so the pin and synthetic transport,
privacy, restart and rotation checks are part of upgrades:

```sh
ATELIER_TEST_COLLECTOR=/path/to/otelcol-contrib .venv/bin/python -m unittest tests.test_observability -q
```

Stop collection without deleting observations:

```sh
launchctl bootout "gui/$(id -u)/com.atelier.observability"
```

### Evidence

- [Codex JSONL](https://learn.chatgpt.com/docs/non-interactive-mode#make-output-machine-readable) defines the scheduled stream, including content-bearing items that must be discarded.
- [Codex hooks](https://learn.chatgpt.com/docs/hooks) and [configuration scope](https://learn.chatgpt.com/docs/config-file/config-reference) define native boundaries and the project-level OTel restriction.
- [Claude monitoring](https://code.claude.com/docs/en/monitoring-usage) defines API events, token buckets, correlation attributes and content/identity controls.
- [Collector transformation](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.160.0/processor/transformprocessor/README.md), [body-only encoding](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.160.0/extension/encoding/jsonlogencodingextension/README.md), and [file rotation](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.160.0/exporter/fileexporter/README.md) define the pinned persistence boundary.
