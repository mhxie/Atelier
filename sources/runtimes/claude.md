## Claude Code capability baseline

Checked: 2026-09-10. Documentation baseline; minimum versions are unknown unless
explicitly stated. Provider, plan, managed policy and host can restrict support.
This file does not establish local activation. Apply the
[evidence contract](README.md) when comparing observations.

| Facet | Documented surface and important conditions | Primary source |
|---|---|---|
| instructions | Root or `.claude/CLAUDE.md`, ancestor guidance, local instructions and nested guidance loaded on access. `.claude/rules` supports recursive Markdown and path-scoped loading. These are context, not deterministic permission enforcement. | [Memory and rules](https://code.claude.com/docs/en/memory) |
| settings | Shared project, project-local, user and managed settings have precedence and field-specific scopes. Managed policy may come from host/MDM/server sources; cloud sessions do not automatically inherit personal local settings. | [Settings](https://code.claude.com/docs/en/settings), [managed settings](https://code.claude.com/docs/en/managed-settings) |
| subagents | `.claude/agents/*.md` supports tool/model/permission/memory/isolation settings and hooks. Parent auto/acceptEdits/bypass modes override child permissionMode; `plan` alone is not a universal hard read-only boundary. Agent hooks require trusted project loading. | [Subagents](https://code.claude.com/docs/en/sub-agents) |
| skills | `.claude/skills`, plugin and other documented native roots support skills and directory symlinks. Automatic discovery of `.agents/skills` is not established by the current official discovery table; format compatibility does not prove path discovery. | [Skill discovery](https://code.claude.com/docs/en/skills#choose-where-skills-load) |
| commands | `.claude/commands/*.md` still produces `/name`; custom commands are part of the skills product model. Invocation controls determine whether a skill is user-only or model-invocable. | [Skills and commands](https://code.claude.com/docs/en/skills) |
| hooks | Registration lives in settings, plugin configuration or skill/agent frontmatter. Handlers explicitly reference scripts; `.claude/hooks/` is optional organization, not automatic registration. | [Hook locations](https://code.claude.com/docs/en/hooks#hook-locations) |
| mcp | Project `.mcp.json`, user/local MCP settings, plugins and managed sources are distinct. Missing project `.mcp.json` does not establish absence of configured MCP. | [MCP scopes](https://code.claude.com/docs/en/mcp#choose-the-right-scope) |
| permissions | `permissions` rules and OS sandboxing differ. `autoMode` classifier lists read user/managed/explicit settings, not project shared/local; local scope exclusion is documented from 2.1.207. These natural-language lists are not deterministic tool-pattern denies. | [Permissions](https://code.claude.com/docs/en/permissions), [auto-mode scope](https://code.claude.com/docs/en/auto-mode-config#where-the-classifier-reads-configuration) |
| output-style | `.claude/output-styles` and `outputStyle` remain supported; `/output-style` is removed from 2.1.91. Custom styles need `keep-coding-instructions: true` to retain coding instructions. | [Output styles](https://code.claude.com/docs/en/output-styles) |
| plugins | `enabledPlugins` selects activation; `extraKnownMarketplaces` declares sources. These are separate from authentication and executable-code trust. | [Marketplaces](https://code.claude.com/docs/en/plugin-marketplaces) |
| workflows | `.claude/workflows/*.js` is native constrained-JavaScript agent orchestration, not ordinary Node: no direct filesystem/shell/module access. Pro needs enablement; workflow authoring requires 2.1.248+. It does not establish durable scheduling. | [Workflows](https://code.claude.com/docs/en/workflows) |
| status-line | A configured command reads JSON and renders display text. Managed hook restrictions may suppress it. Context-size fields are not cumulative consumption; display does not provide persistent telemetry storage. | [Status line](https://code.claude.com/docs/en/statusline) |
| memory | Auto memory is enabled by default and shared across a repository's worktrees. It differs from explicit guidance and saved conversations; it is not the canonical Atelier knowledge store. | [Auto memory](https://code.claude.com/docs/en/memory) |
| sessions | Resume restores conversation, not interrupted tools or background processes. Branch/fork-session creates conversation identity, not filesystem isolation; session-only permission grants do not carry into a fork. Histories differ by host. | [Sessions](https://code.claude.com/docs/en/sessions) |
| isolation | CLI worktrees and subagent `isolation: worktree` provide separate Git checkouts, not OS sandboxes. Ignored/untracked content is absent by default; `.worktreeinclude` can opt ignored files into copying. External effects and credentials are not isolated. | [Worktrees](https://code.claude.com/docs/en/worktrees) |
| checkpoints | Rewind covers tracked Claude edits/conversation; Bash writes, external changes and ordinary subagent edits are outside normal coverage. Foreground forked skills have an exception. Rewind cannot undo messages, pushes or cloud mutations. | [Checkpointing](https://code.claude.com/docs/en/checkpointing) |
| collaboration | Agent teams are experimental, disabled by default and interactive-only, not `-p`/SDK. Team resume/rewind limitations and changed named-Agent behavior require separate evaluation from ordinary subagents. | [Agent teams](https://code.claude.com/docs/en/agent-teams) |
| observability | Opt-in OTel provides usage/events; span tracing has a separate beta gate. Content logging defaults off, but metadata may contain account identifiers. `subagent_completed.total_tokens` is final-context footprint, not cumulative consumption; use attributed counters. | [Monitoring usage](https://code.claude.com/docs/en/monitoring-usage) |
| channels-monitors | Channels is research-preview MCP delivery to an open session, with auth/provider/organization restrictions. Monitor follows command output or WebSockets under permissions, is session-bound and does not survive resume; WebSockets are documented from 2.1.195. Neither is durable scheduling. | [Channels](https://code.claude.com/docs/en/channels), [Monitor](https://code.claude.com/docs/en/tools-reference#monitor-tool) |

Maintenance entrypoints: [changelog](https://code.claude.com/docs/en/changelog),
[what's new](https://code.claude.com/docs/en/whats-new/index),
[documentation index](https://code.claude.com/docs/llms.txt), and
[feature availability](https://code.claude.com/docs/en/feature-availability).
Recheck scope restrictions and defaults as well as feature additions. Native
workflow scripts, teams and session monitors have different durability and
permission contracts; their existence alone does not justify replacing an owner.
