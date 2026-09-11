# Runtime Adapters Protocol

Atelier should run under Claude Code and Codex without forking the reflection
system. The core idea is to separate four concerns:

| Concern | Owned by | Example |
|---|---|---|
| Workflow | `skills/`, `protocols/` | `/hi`, `/weekly`, `/review` |
| Role | `harness/agents.toml`, `agents/` | Researcher, Synthesizer, Reviewer |
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

Private user skills are an exception to the committed project-edge layout.
Their canonical source is `<paths.private_skills>/<name>/SKILL.md`, and
native symlinks expose that same directory through the user-level Claude and
Codex skill roots. Private names never enter `skills.toml`,
`intents.toml`, or committed runtime adapters. The full ownership and
activation contract is in `protocols/components.md`.

`.claude/skills/` is a Claude Code-only surface holding **entry hints**, not authoritative dispatch. Claude Code matches a skill's frontmatter description against user phrasing semantically: the LLM judges relevance, not substring. On a match the skill forwards into `/hi`; the canonical intent catalog in `harness/intents.toml` is still the single decision point for which agents run. Codex does not read `.claude/skills/`; repo-scoped skills under `.agents/skills/` provide its native entry surface. `$atelier` handles broad routing and explicit workflows read the matching canonical `skills/*/SKILL.md`. Skill exposure is additive at both runtime edges and produces zero workflow duplication.

`scripts/harness_lint.py` enforces structural invariants only: skill name matches its directory, frontmatter has a non-empty description that mentions `/hi` (delegation), and the skill name corresponds to an existing `intents.<name>` row. Coherence between the skill's prose description and the intent it exposes is human-curated — substring-checking an LLM-judged trigger surface would be the wrong tool.

The canonical skill files are provider-neutral, and both runtimes have native
execution edges. Claude Code consumes `AskUserQuestion` and
`Agent(...)` directly. Codex maps them to its available choice UI and the
project agents under `.codex/agents/`, falling back to numbered questions or
sequential role emulation only when the active surface lacks those features.

`harness/skills.toml` and `harness/agents.toml` are the registries shared by
both runtimes. They map portable names to `skills/` and `agents/` sources.
Generated Claude and Codex edges point directly to those sources;
`scripts/harness_lint.py` enforces the mapping.

Codex reserves slash-prefixed input for built-in TUI commands. Its native
repo-shared counterpart is an explicit `$skill` mention: Claude `/weekly` maps
to Codex `$weekly`, `/hi` maps to `$hi`, and so on. Each project skill is
explicit-only (`allow_implicit_invocation: false`) and reads its canonical
source directly. Interactive use does not launch a
helper process. From an external shell, quote the skill mention, for example
`codex -C . '$weekly'`. When a Claude-shaped workflow tells the user to invoke
another registered project skill, Codex renders the `$skill` form. Native
Codex built-ins such as `/hooks` keep their slash form.

Lifecycle hooks live in `.codex/hooks.json` and `.claude/settings.json`.
Session cues and locks use `scripts/cues.py`; `invocation_log_gc.py` ages out
existing direct-API payload logs. Opt-in hooks send advisory identifiers only;
native usage, privacy filtering, and coverage rules live in the
[observability reference](../sources/runtimes/observability.md).

Claude Code also loads the `hooks:` block of an agent's frontmatter and runs
those hooks for that agent's own tool calls, so a boundary can be scoped to
one role: the Reviewer's read-only Bash guard (`scripts/readonly_bash_guard.py`)
is a `PreToolUse` hook there. The guard is an explicit allowlist of reading
commands that denies everything it does not describe, and fails closed on a
payload or shell shape it cannot read. Agent-scoped hooks have no Codex equivalent;
there the role source's prose rule is the only boundary.

## Runtime Selection

Runtime references and local CLI discovery follow the
[evidence contract](../sources/runtimes/README.md). The registry owns bindings;
private review owns adoption and retirement, not the discovery module.

`harness/runtimes.toml` declares both native CLI surfaces and ships with Codex
as the default. `scripts/atelier_runtime.py` is an optional selector around
those surfaces. It never expands a workflow into an adapter prompt: it sends
the registered name directly as `$<skill>` to Codex or `/<skill>` to
Claude Code.

Resolution order is:

1. `--runtime codex|claude` for one selector invocation.
2. `ATELIER_RUNTIME=codex|claude` for one interactive launcher process.
3. Gitignored `harness/runtime.local.toml`, written by
   `python3 scripts/atelier_runtime.py use <runtime>`.
4. The committed Codex default in `harness/runtimes.toml`.

Direct CLI invocation always remains valid. The selector exists for interactive
launches. Unattended local routines intentionally do not use this resolution
chain. launchd keeps a self-hosted Prefect server and deployment runner alive;
Prefect owns schedules, run state, history, concurrency, and eligible retry
timing. `scripts/routine_adapter.py` owns the fixed headless-Codex arguments,
sanitized environment, profile boundary, prompt, and verified domain receipt.
Scheduled model work has no runtime fallback.

Autoevo is the narrow exception to the ordinary vault launch. Its adapter
selects the pinned `atelier-autoevo-draft` permission profile and equivalent
legacy workspace-only flags; a managed-profile rejection fails closed. Both
paths permit `:root` reads and scratch-workspace writes only, keep its control
directories read-only, disable network, and omit vault `--add-dir`. The model
uses retained plan/snapshots, a staged QMD database,
and cached GGUF files read-only, then authors only `proposal.json` plus its
transport acknowledgment. The trusted parent publishes and records one
canonical JSON domain result; Markdown is derived and Prefect owns run state.

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
`protocols/agent-handoff.md`. The agent-to-voices mapping
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

## Capability Profiles

`harness/capabilities.toml` owns the runtime-neutral role capabilities and
their concrete tool mappings. Do not copy its inventory into guidance.

Routine profiles in `harness/routine_profiles.toml` are a separate execution
envelope, not additions to this role-capability vocabulary. Their permission
strings are action allowlists for archived scheduled procedures, while fields
such as `sandbox`, `atelier_access`, and `allowed_adapters` are enforced by the
Codex-only local runner. Cloud rows describe connector requirements for manual
ChatGPT Scheduled handoff. Do not add those action strings to
`harness/capabilities.toml` unless an interactive agent role begins depending
on a new provider-neutral capability.

## Codex Skill Execution

`AGENTS.md` owns native invocation, tool translation, and role fallback;
`CLAUDE.md` owns retrieval and write boundaries, including Scribe and bounded
operational-artifact exceptions. Generated `$skill` edges load both before
the selected canonical skill. Keep launch recipes in user-level CLI
documentation, not the always-loaded adapter.
