# Codex

Codex runs against the Atelier system. It reads the root `AGENTS.md` and
discovers repo-scoped skills under `.agents/skills/`.
The current Claude Code command files remain the workflow specs. Codex adapts
them through `protocols/runtime-adapters.md`, exposes roles through
`.codex/agents/`, and reuses lifecycle scripts through `.codex/hooks.json`.

Start an interactive Codex session from the repo root:

```bash
codex -C . --sandbox workspace-write
```

Atelier's shared runtime selector also ships with Codex selected:

```bash
python3 scripts/atelier_runtime.py status
python3 scripts/atelier_runtime.py run hi
```

The selector only launches the native CLI surface. It does not generate a
prompt or start a nested process inside an active Codex thread. A user can
persist Claude as the launcher and launchd default with
`python3 scripts/atelier_runtime.py use claude`.

Inside an active Codex thread, use explicit skills as the counterpart to
Claude slash commands:

```text
$hi
$hi context
$reflect
$weekly
$lint
```

Each skill reads the matching `.claude/commands/*.md` source directly and
executes it in the current thread. No Python command bridge is required.

From an external shell or automation, launch Codex directly with the skill
mention quoted from the shell:

```bash
codex -C . '$hi'
codex -C . '$hi context'
codex exec -C . '$lint'
```

Codex discovers native roles from `.codex/agents/*.toml`. The adapters remain
thin: `harness/agents.toml` supplies discovery descriptions and each adapter
loads its authoritative `.claude/agents/*.md` role brief. Inspect active
subagents with `/agent` in the CLI.

Native hooks provide session cues, session-lock refresh, out-of-band intent
miss logging, and retirement cleanup for legacy direct-API invocation logs.
New direct calls are not logged. On the first session after checkout or after
a hook change, open `/hooks` and trust the project definitions.

Literal project slash commands are unavailable because the Codex TUI owns the
slash namespace. Use the corresponding explicit skill:

```text
$hi
$hi context
$weekly
$review
```

Type `$` in the composer to browse command skills. Inspect native roles with
`/agent` in the CLI.

Code review:

```bash
codex review --uncommitted
```

Before harness changes are finished:

```bash
python3 scripts/harness_lint.py
```
