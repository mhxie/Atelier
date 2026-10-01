---
name: atelier
description: Run or modify Atelier skills, routines, agents, tools, intent routing, and harness portability in this repo. Use for broad Atelier harness work or to adapt Claude `/hi` to native Codex `$hi`.
---

## Atelier

Use this skill when the user asks to run or modify Atelier workflows,
components, or harness portability. It is the one hand-written Codex edge file; it
points at the canonical sources and adds only what is Codex-native.

## Quick Start

1. Read `AGENTS.md` for the shared contract, runtime adaptations, and harness
   checklist. It is the single instruction source for Claude Code and Codex.
2. Read `protocols/runtime-adapters.md` only when changing or debugging
   cross-runtime behavior.
3. Invoke known workflows through their explicit repo skills (`$hi`, `$weekly`,
   `$review`, `$triage`, `$lint`, and so on). Each reads the matching
   `skills/<name>/SKILL.md` source and runs it in the current thread.
   For `$hi`, classify the request against `scripts/intent_coverage.py
   catalog` and read only the selected row's `procedure`. No fit is a
   semantic handoff through `intents.general`, never implicit reflection.
4. Do not launch Codex recursively, and do not launch bot-invoked workflows
   such as `autoevo-nightly` from shorthand.
5. Discover native roles under `.codex/agents/` and inspect them with `/agent`.
   `Agent(...)` in a command means dispatch the matching project agent; if
   dispatch is unavailable, run the role sequentially from
   `agents/<role>.md` and disclose the downgrade.
6. For external launches, `scripts/atelier_runtime.py` resolves the committed
   Codex default from `harness/runtimes.toml`, the gitignored local
   preference, and one-process overrides.

## Codex-native notes

- Codex CLI slash input is reserved for built-in TUI commands; project commands
  are `$name` skills with `allow_implicit_invocation: false`. `$reflect` runs
  `$hi`. Translate `/name` references to `$name`; never translate real Codex
  built-ins such as `/hooks` or `/agent`.
- `Read` is the local file, `Grep`/`Glob` are `rg` and `rg --files`, `Bash` is
  the local shell, `AskUserQuestion` is a native choice UI or a concise
  numbered question.
- Type `$` in the Codex composer for command discovery. Do not guess an
  unavailable command.

## Operations

- Local retrieval uses `scripts/semantic.py` with the pinned QMD dependency.
  `sources/semantic.md` owns setup, scopes, and the bounded JSON contract.
  Queries use cached models only; downloads require explicit initialization.
- Linked ledger tasks use `scripts/todos.py check` and `sync --file <quarter>.md`
  (preview; `--apply` writes); see `protocols/local-first-architecture.md`.
- Route context packs selected repository profiles and registered session logs
  with pinned Repomix; `protocols/session-continuity.md` owns selection and ceilings.
- Reflection's energy and exploration intents share
  `skills/daily-reflection/SKILL.md` while keeping their selected context.
  That procedure and `protocols/session-log.md` own the branches and compact logs.
- `$digest` uses `collect --json` then `write` with preinstalled markdown-it-py
  and local MJML; optional quota comes from CodexBar OAuth JSON. The shared
  command owns setup, offline boundaries, and the three-edition overdue TODO limit; never sync during a run.
- Reading feedback connects Curate, Read, Introspect, and explicit policy evaluation through
  `protocols/decision-ledger.md` → Reading feedback loop. Use its typed
  `decisions.py` helpers for compact event batches and stored policy IDs;
  load Offline reading evaluation only for policy experiments.
- Dispatch inputs and completion reporting live in `protocols/agent-handoff.md`;
  load its common sections and the selected payload contract. The procedure owns
  calls; `protocols/agent-handoff.md` also owns responsibilities and voice legs.
  Reader owns the shared reading behavior; Scholar keeps only its role settings
  and the shared-contract pointer.
- Direct chat-completion calls are unlogged. Runtime hooks only age out legacy
  full-payload invocation logs through `scripts/invocation_log_gc.py`; opt-in
  native usage/lifecycle observation is documented in `sources/runtimes/observability.md`.
- For runtime capability maintenance, read `sources/runtimes/README.md` and
  the registry's references; CLI discovery does not prove activation.

- Local scheduled routines: validate declarations with
  `uv run --frozen python scripts/routine_prefect.py validate --json` and read
  recent Prefect state with `uv run --frozen python scripts/routine_status.py`.
  `scripts/cron_spec.py` uses Prefect's cron engine for health-check dates and cadence.
  The transfer procedure is in `scripts/launchd/README.md`: stop every source
  scheduler before loading the Prefect deployment service. Runtime and adapter
  ownership is documented in `protocols/runtime-adapters.md`; ordinary artifact
  evidence and shared receipt validation live in `protocols/remote-routines.md`.
  Connector discovery before reporting missing inputs follows
  `routines/_adapters/archived-prompt/PROCEDURE.md`.
  Autoevo drafts only in its isolated workspace; the trusted parent owns
  publication and its single structured result (`protocols/autoevo.md`).
- Private routine mappings, tools, routines, digest declarations, and skills
  stay under their registered `$OV` roots. Classification and activation live
  in `protocols/components.md`; remote execution and digest context live
  in `protocols/remote-routines.md`.
- `$civ` loads framework definitions from the private source referenced by the profile.

## Harness Changes

Follow the checklist in `AGENTS.md` and `harness/README.md`. The registries
are `harness/skills.toml`, `harness/agents.toml`, `harness/intents.toml`,
`harness/models.toml`, `harness/capabilities.toml`, `harness/retrieval.toml`, `harness/paths.toml`, and
`harness/runtimes.toml`, and `routines/registry.toml`; edit them, never the
generated runtime edges. Run `python3 scripts/harness_lint.py` before finishing and
`scripts/harness_smoke.py` after helper or registry edits. Keep runtime edges
thin: they point to canonical sources and do not copy workflow bodies.
Public configuration shape lives in `harness/registry.schema.json`; lint uses
the pinned validator in the project `.venv`. Setup and error codes are documented
in `harness/README.md`; lint never installs missing dependencies itself.

Before edits, read `protocols/repo-conventions.md` → Editing discipline for
whole-feature budgets, approval thresholds, smoke scope, and cost reporting.
