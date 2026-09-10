## Runtime capability evidence

Read this directory when reviewing runtime changes or replacing an Atelier
mechanism. These are dated public-source baselines, not activation instructions.
The facet IDs in [Codex](codex.md) and [Claude Code](claude.md) align for comparison;
an undocumented equivalent remains unknown, not impossible.

| Owner | Responsibility |
|---|---|
| This directory | Upstream facts, primary sources, host/version conditions and uncertainty |
| `harness/runtimes.toml` | Atelier's declared native bindings and reference pointers |
| `scripts/runtime/capabilities.py` | Bounded local CLI discovery, shared by existing callers |
| Private review procedure and its existing reports | Local fit, comparisons, decisions and follow-up |

`harness/capabilities.toml` remains the abstract role/tool vocabulary. It is not
a product-feature inventory. Domain owners retain scheduling, artifact
verification and authorization; a new native feature does not transfer them.

## Local evidence contract

`python3 scripts/atelier_runtime.py status --capabilities --json` emits schema 1.
Ordinary `status` does not probe. Local routines can opt in with
`runtime_snapshot = true`; the existing adapter stages the same JSON at
`ATELIER_RUNTIME_SNAPSHOT`, adding the validated `cycle_id` for that invocation,
and removes it afterwards. This is not a reusable cache: a missing or different
cycle is stale evidence. Observation time records the actual run, not its due date.

The snapshot contains its UTC observation time and, for each standalone CLI:

- A strictly parsed numeric version or null, plus a controlled probe status.
- A fixed set of help-advertised commands/options: `advertised`,
  `not-advertised`, or `unknown`. Absence from help does not prove removal.
- The public baseline's path, presence and content hash, declared integration
  names, and existence of declared project surfaces. Existence is not loading.
- `effective_configuration`, `enabled`, `usable` and `verified_in_use`, each
  unknown: version/help discovery cannot establish these.

Only fixed native version/help arguments execute, with five seconds and 64 KiB
per probe. The helper drains stdout in memory and terminates its process group
without additional grace on timeout/overflow. Shared child tracking also cleans
up on host-owned interpreter exit. Raw output, stderr and exception text are never returned
or persisted. It reads no auth, history, configuration bodies or private data,
performs no web requests, and starts no model. It inherits only PATH/HOME and a
fixed locale. PATH identifies the standalone CLI, not a desktop-bundled build.
Expected discovery failures become explicit statuses. Staging failures stop
before a model attempt; existing retry and receipt rules remain unchanged.

## Updating a baseline

Record the facet, documented status, official source, checked date, version
boundary when stated, host/provider/scope conditions and unresolved questions.
Do not guess introduction versions. Distinguish `documented`,
`not-documented`, `deprecated-supported`, `removed` and `unknown`; experimental
or preview maturity is independent of installation, activation and validation.

Start with both vendors' release notes and documentation indexes, then open
the feature reference supporting a changed claim. Installed-version changes
require rechecking relevant assumptions. Resolve documentation conflicts with
version-matched release notes and bounded local evidence; retain uncertainty
when they disagree. Retrieval failure is a coverage gap, not no change.

Compare each change with the actual Atelier owner and its requirements.
Evaluate native support first, then maintained third-party options, then the
uncovered need for custom code. Replacements must preserve permissions,
unattended behavior, privacy, failure recovery and domain outcomes, with tests,
rollback and an explicit old-implementation removal condition. Research
produces proposals in the existing private report, never approval, automatic
installation, a new scheduler, or another decision ledger.
