---
name: atelier
description: Run or modify Atelier workflows, commands, agents, intent routing, and harness portability in this repo. Use for broad Atelier harness work or to adapt Claude commands such as `/hi` to native Codex skills such as `$hi`.
---

## Atelier

Use this skill when the user asks to run or modify Atelier workflows, commands,
agents, or harness portability. It is the one hand-written Codex edge file; it
points at the canonical sources and adds only what is Codex-native.

## Quick Start

1. Read `AGENTS.md` (Codex adaptation rules, harness-change checklist) and
   `CLAUDE.md` (always-on invariants, write boundaries). Do not restate them
   here; they are the source.
2. Read `protocols/runtime-adapters.md` only when changing or debugging
   cross-runtime behavior.
3. Invoke known commands through their explicit repo skills (`$hi`, `$weekly`,
   `$review`, `$triage`, `$lint`, and so on). Each reads the matching
   `.claude/commands/<command>.md` source and runs it in the current thread.
   For `$hi`, classify the request against `scripts/intent_coverage.py
   catalog` and read only the selected row's `procedure`. No fit is a
   semantic handoff through `intents.general`, never implicit reflection.
4. Do not launch Codex recursively, and do not launch bot-invoked workflows
   such as `autoevo-nightly` from shorthand.
5. Discover native roles under `.codex/agents/` and inspect them with `/agent`.
   `Agent(...)` in a command means dispatch the matching project agent; if
   dispatch is unavailable, run the role sequentially from
   `.claude/agents/<role>.md` and disclose the downgrade.
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

- Local scheduled routines: `uv run scripts/routine_owner.py status`; the
  transfer procedure is in `scripts/launchd/README.md` (unload the source
  scheduler first, then `claim --force --source-stopped`). Run
  `python3 scripts/routine_audit.py audit --check-system --json` before
  enabling or handing off launchd jobs.
- Private routine mappings, digest ledger declarations, and private skill
  sources stay under `$OV` and `<paths.private_features>/`; the contracts are
  `protocols/remote-routines.md` and `protocols/private-features.md`.

## Harness Changes

Follow the checklist in `AGENTS.md` and `harness/README.md`. The registries
are `harness/commands.toml`, `harness/agents.toml`, `harness/intents.toml`,
`harness/models.toml`, `harness/capabilities.toml`, `harness/paths.toml`, and
`harness/runtimes.toml`; edit them, never the generated `.codex/` or
`.agents/` files. Run `python3 scripts/harness_lint.py` before finishing and
`scripts/harness_smoke.py` after helper or registry edits. Keep command
skills thin: they point to shared Claude command specifications and must not
copy workflow bodies into the Codex edge.
