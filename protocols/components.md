# Component Taxonomy

Every user-owned component has exactly one kind and one visibility:

| Kind | Contract | Canonical shape |
|---|---|---|
| Skill | User-invoked, context-sensitive workflow | `<root>/<name>/SKILL.md` |
| Routine | Scheduled or recurrent Prefect workflow | one `[[routine]]` registry row; package optional |
| Agent | Reusable judgment role delegated by a workflow | `<root>/<name>.md` |
| Tool | Deterministic primitive called by a person or workflow | `<root>/<name>/README.md` plus executable code |

`public | private` is the visibility axis. Orthogonal axes are
`home-global | atelier-project` scope, `canonical | generated | runtime`
materialization, and `Claude | Codex | Prefect` runtime. External/vendor is
provenance. Intents route and protocols constrain; neither is a component kind.

## Canonical roots

| Scope and visibility | Skills | Agents | Routines | Tools |
|---|---|---|---|---|
| Atelier public | `skills/` | `agents/` | `routines/registry.toml` | `tools/` when present |
| Atelier private | `<paths.private_skills>/` | `<paths.private_agents>/` | `<paths.private_routines>/registry.toml` | `<paths.private_tools>/` |
| Home-global private | `~/.claude/skills/` | `~/.claude/agents/` | none currently | `~/.claude/tools/` when present |

Public roots are Git-backed; private `_tools` roots stay out of semantic
retrieval. Home-global source stays separate and private until published.

Only skills enter skill discovery. `.claude/commands/`, `.claude/agents/`,
`.agents/skills/`, and `.codex/agents/` are generated Atelier runtime edges.
Other kinds do not enter skill discovery. A scheduled tool is a routine
dependency; `model | process` is runner metadata, not kind.

## Private skill activation

| Runtime | Default target |
|---|---|
| Claude Code | `~/.claude/skills/<skill-name>` |
| Codex | `~/.agents/skills/<skill-name>` |

Resolve `<paths.private_skills>`, then link both runtimes to the same source:

```bash
ln -s "<resolved-private-skills-root>/<name>" "$HOME/.claude/skills/<name>"
ln -s "<resolved-private-skills-root>/<name>" "$HOME/.agents/skills/<name>"
```

Never replace a directory or unrelated symlink. The canonical synchronizer can
derive the Codex link from the Claude link. Invocation policy stays runtime-local.

## Public and private movement

Activation and publication are separate:

- Activation links runtimes; unlinking preserves source.
- Publication needs a reviewed public copy, public dependencies, and both gates.
- Privatization removes the public tip and installs private source. Rewrite
  history only if historical availability is unacceptable.

Keep credentials in private configuration or a runtime credential provider.
