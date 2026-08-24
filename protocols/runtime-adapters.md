# Runtime Adapters Protocol

Atelier should run under Claude Code and Codex without forking the reflection
system. The core idea is to separate four concerns:

| Concern | Owned by | Example |
|---|---|---|
| Workflow | `protocols/`, command specs | `/hi`, `/weekly`, `/review` |
| Role | `harness/agents.toml`, agent specs | Researcher, Synthesizer, Reviewer |
| Capability | `harness/capabilities.toml` | `semantic_query`, `write_local_file`, `web_search` |
| Runtime and model | adapters, local CLI config + `profile/models.toml` (gitignored) | one runtime per call, model bound per profile |

This follows the OpenClaw lesson: the system can use different models when the
provider and runtime are explicit metadata, not assumptions buried inside the
workflow.

## Runtime Surfaces

| Runtime | Reads | Native surface | Status |
|---|---|---|---|
| Codex | `AGENTS.md` | `.agents/skills/`, `.codex/agents/`, `.codex/hooks.json`, Codex CLI and review | First-class native harness; shipped default |
| Claude Code | `CLAUDE.md` | `.claude/agents/`, `.claude/commands/`, `.claude/skills/` (entry hints only; not authoritative dispatch) | First-class native harness; selectable default |

Private user features are an exception to the committed project-edge layout.
Their canonical source is `<paths.private_features>/<name>/SKILL.md`, and
native symlinks expose that same directory through the user-level Claude and
Codex skill roots. Private names never enter `commands.toml`,
`intents.toml`, or committed runtime adapters. The full ownership and
activation contract is in `protocols/private-features.md`.

`.claude/skills/` is a Claude Code-only surface holding **entry hints**, not authoritative dispatch. Claude Code matches a skill's frontmatter description against user phrasing semantically: the LLM judges relevance, not substring. On a match the skill forwards into `/hi`; the canonical intent router in `harness/intents.toml` is still the single decision point for which agents run. Codex does not read `.claude/skills/`; repo-scoped skills under `.agents/skills/` provide its native entry surface. `$atelier` handles broad routing and harness work, while explicit command skills such as `$weekly` and `$review` read the matching `.claude/commands/*.md` specification directly. Skill exposure is additive at both runtime edges and produces zero workflow duplication.

`scripts/harness_lint.py` enforces structural invariants only: skill name matches its directory, frontmatter has a non-empty description that mentions `/hi` (delegation), and the skill name corresponds to an existing `intents.<name>` row. Coherence between the skill's prose description and the intent it exposes is human-curated — substring-checking an LLM-judged trigger surface would be the wrong tool.

The command files remain Claude-shaped source specifications, but both runtimes
have native execution edges. Claude Code consumes `AskUserQuestion` and
`Agent(...)` directly. Codex maps them to its available choice UI and the
project agents under `.codex/agents/`, falling back to numbered questions or
sequential role emulation only when the active surface lacks those features.

`harness/commands.toml` and `harness/agents.toml` are the registries shared by
both runtimes. They map portable names to the current Claude source files.
Codex command skills and agent TOMLs point directly to those sources;
`scripts/harness_lint.py` enforces the mapping.

Codex reserves slash-prefixed input for built-in TUI commands. Its native
repo-shared counterpart is an explicit `$skill` mention: Claude `/weekly` maps
to Codex `$weekly`, `/hi` maps to `$hi`, and so on. Each command skill is
explicit-only (`allow_implicit_invocation: false`) and reads its authoritative
Claude command specification directly. Interactive use does not launch a
helper process. From an external shell, quote the skill mention, for example
`codex -C . '$weekly'`. When a Claude-shaped workflow tells the user to invoke
another registered project command, Codex renders the `$command` form. Native
Codex built-ins such as `/hooks` keep their slash form.

Codex lifecycle hooks live in `.codex/hooks.json`. `SessionStart` reuses
`scripts/cues.py --hook --runtime codex`; `UserPromptSubmit` optionally records
the replay prompt, refreshes the session lock, and runs
`scripts/intent_coverage.py intent-hook --runtime codex`; `Stop` optionally
reconciles the replay snapshot and runs shared shadow-log cleanup. Claude Code
keeps the corresponding behavior in `.claude/settings.json`, using both `Stop`
and `SessionEnd` for optional replay reconciliation. Replay capture is disabled
by default. Both edges always call the shared script, which resolves the
machine-local Atelier preference and optional process override. The canonical
activation contract is in `protocols/session-replay.md`.

## Runtime Selection

`harness/runtimes.toml` declares both native CLI surfaces and ships with Codex
as the default. `scripts/atelier_runtime.py` is an optional selector around
those surfaces. It never expands a workflow into an adapter prompt: it sends
the registered name directly as `$<command>` to Codex or `/<command>` to
Claude Code.

Resolution order is:

1. `--runtime codex|claude` for one selector invocation.
2. `ATELIER_RUNTIME=codex|claude` for one interactive launcher process.
3. Gitignored `harness/runtime.local.toml`, written by
   `python3 scripts/atelier_runtime.py use <runtime>`.
4. The committed Codex default in `harness/runtimes.toml`.

Direct CLI invocation always remains valid. The selector exists for interactive
launches. Unattended local routines intentionally do not use this resolution
chain: `scripts/routine_runner.sh` fixes them to Codex because their sandbox,
plugin loading, sanitized environment, and approval policy are implemented and
tested at that runtime edge.

## Plugins and Permissions

The canonical write path is local: the runtime writes files under `$OV/`, and
a filesystem sync client (such as Google Drive) handles persistence. When
`$OV/` is outside the workspace, add it as a writable root while keeping the
sandbox at workspace-write:

```bash
codex -C . --add-dir "$OV" --sandbox workspace-write --ask-for-approval on-request '$hi'
```

Write access being technically possible does not bypass domain rules: ordinary
`$OV/` writes still require approval, and daily notes remain user-authored
except for verbatim Scribe capture. Beyond repo + `$OV` read/write and local
shell (`uv`, `rg`, `git`, `jq`), everything is optional: live web search for
the research agents (`--search`), outbound shell network for the Readwise CLI.

No plugin is required; plugins only add access to cloud data not already on
disk:

| Integration | Authorization | Supported use |
|---|---|---|
| Gmail plugin | plugin enabled + Google OAuth | mail search/read for user-requested context |
| Google Drive plugin | plugin enabled + Google OAuth | cloud-only Drive files; Drive-writing routines need the connector on their hosting runtime |
| Readwise CLI | `readwise login` or token | Reader search, saved documents, inbox curation, anchor snapshots |
| GitHub plugin | connector auth | remote issues/PRs; local `git` works without it |
| Google Calendar plugin | Google OAuth | fork-added calendar workflows; no core command depends on it |

Plugin readiness has four gates (installed, enabled, OAuth completed, tools
loaded in a fresh session), and each runtime manages its own connections:
authorizing a service on Claude.ai does not configure the Codex plugin, or
vice versa. References: [Codex plugins](https://learn.chatgpt.com/docs/plugins.md),
[sandbox and approvals](https://learn.chatgpt.com/docs/agent-approvals-security.md),
[MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).

## Session replay

When replay is enabled through the machine-local preference or process
override, both runtime edges capture each user input before routing and
reconcile a private native-transcript snapshot after work stops. Capture is off
by default. The shared contract, storage boundary, privacy guard, and recovery
procedure are in `protocols/session-replay.md`. This archive is operational
evidence for deferred bot-only re-analysis, never ambient model context or a
replacement for user-facing reflections.

## Provider-Neutral Rules

- Do not add new provider-specific model names to shared protocols. Use a model
  profile from `harness/models.toml`.
- Do not add new provider-specific tool names to shared protocols. Use a
  capability from `harness/capabilities.toml`.
- Existing `.claude/` files may keep Claude frontmatter and tool names. They are
  adapter surfaces.
- New shared docs should say "run a semantic query" or "write a local file",
  not name provider-specific tools, unless they are documenting an adapter
  itself.
- If a runtime lacks a feature, degrade explicitly. Example: if Codex cannot
  spawn the registered project agent in a given environment, read the target
  agent spec and run the step sequentially.

## Model Profiles

Agent roles ask for capability classes, not fixed provider models. Profile
schema (identity names and runtime-neutral reasoning tiers) is defined in
`harness/models.toml` (committed); the
actual provider/model bindings (model id, endpoint URL, env var, request
extras) live in `profile/models.toml` (gitignored). Loaders merge schema +
bindings at runtime.

Voice dispatch model: the single source of truth is
`protocols/voice-dispatch.md`. The agent-to-voices mapping
lives in `harness/agents.toml` as a `voices` keyed inline table per agent
(`{native = "...", direct = "..."}` or single-leg variants). `native` means
the selected runtime's project-agent surface, not Claude specifically. Claude
resolves its concrete model from agent frontmatter; Codex agents inherit the
selected Codex model unless their project adapter pins a model. The shared
`reasoning_tier` maps to Codex `model_reasoning_effort` at the adapter edge:
`light → low`, `balanced → medium`, `deep → high`, and `xdeep → xhigh`.
Sonnet execution and retrieval roles use `xdeep`; they never silently inherit
a lower Codex effort.
External provider bindings remain in gitignored `profile/models.toml`.
Shadow telemetry resolves native identity through
`scripts/shadow.py native-model`: Claude uses the role binding, while Codex
uses the dynamic `codex_native` slot so it never inherits an Anthropic cost
row.

## Capability Profiles

Capabilities describe what an agent needs, independent of the runtime:

- `read_file`
- `search_text`
- `run_shell`
- `semantic_query`
- `web_search`
- `web_fetch`
- `write_local_file`
- `spawn_role` (native `.codex/agents/<role>.toml`, sequential fallback)
- `ask_user`

The concrete tool mapping is in `harness/capabilities.toml`.

Routine profiles in `harness/routine_profiles.toml` are a separate execution
envelope, not additions to this role-capability vocabulary. Their permission
strings are action allowlists for archived scheduled procedures, while fields
such as `sandbox`, `atelier_access`, and `allowed_commands` are enforced by the
Codex-only local runner. Cloud rows describe connector requirements for manual
ChatGPT Scheduled handoff. Do not add those action strings to
`harness/capabilities.toml` unless an interactive agent role begins depending
on a new provider-neutral capability.

## Codex Command Execution

When a user asks Codex to run an Atelier command:

1. Read `AGENTS.md`.
2. Read `CLAUDE.md` for domain rules and safety constraints.
3. Read `.claude/commands/<command>.md` for the workflow.
4. Translate Claude-specific constructs using the table in `AGENTS.md` § Codex Adaptation.
5. Dispatch referenced roles through `.codex/agents/<role>.toml`; the adapter
   instructs the subagent to read the authoritative `.claude/agents/` brief.
   If subagents are unavailable, emulate the brief sequentially and disclose it.
6. Prefer local `$OV/` files, `rg`, and `uv run scripts/semantic.py`.
7. Ask before user-facing note writes under `$OV/`. Scribe capture operations
   (`daily_note`, `dining_row`, `gtd_entry`, `people_stub`, `generic`) write
   directly because the user already authored the raw content. Bounded private
   operational artifacts defined by `protocols/session-log.md` or
   `protocols/session-replay.md` also write without approval. Other agents and
   ad-hoc orchestrator writes still ask first.
8. Report any downgraded capability, such as missing web access or unavailable
   subagent dispatch.

For command invocation, use the native `/name` or `$name` surface described
above. Keep launch recipes in user-level CLI documentation rather than the
always-loaded project adapter.
