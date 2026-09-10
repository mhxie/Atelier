# Atelier

> **A personal workshop, published.** A reflective-thinking system for [Codex CLI](https://github.com/openai/codex), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), and a local-first Zettelkasten: daily reflection, decision journals, deep reading, goal tracking, knowledge crystallization. Not a product. The patterns are reusable; the configuration is bespoke.

The system surrounds an **œuvre**: notes, decisions, and reflections kept as local Markdown under `$OV/`, outside this repository. Task-specific agents run the sessions, a deterministic trust engine scores the wiki layer, and shared registries drive both runtimes. This page is the map, not a workflow specification.

## Install

```bash
git clone https://github.com/mhxie/atelier.git ~/atelier
cd ~/atelier
uv sync
echo 'export OV="$HOME/path/to/your/vault"' >> ~/.zshrc
source ~/.zshrc
```

Personal content under `$OV/` is gitignored; only system configuration is committed.

## Run

```bash
codex -C . --add-dir "$OV" '$hi'   # Codex, the shipped default
claude                              # Claude Code: /introspect once, then /hi
```

Command names are stable across runtimes (`$hi` in Codex is `/hi` in Claude Code). `$hi` opens the session menu; `$introspect` builds `profile/` from your notes and comes first on a fresh vault. A fresh clone has no vault and no profile: an onboarding cliff, working as intended; this is the maintainer's daily-use configuration, not a turnkey second brain.

## Map

| Want | Read |
|---|---|
| The load-bearing idea: directory = certification tier (L1–L5) | `protocols/local-first-architecture.md` |
| Claim-level trust: `[C1]` markers, bi-temporal anchors, PageRank seeded by external evidence | `protocols/wiki-schema.md`, `scripts/trust.py` |
| Provider-neutral registries: commands, agents, models, capabilities, paths | `harness/README.md` |
| How two runtimes share one spec; plugins, sandbox, permissions | `protocols/runtime-adapters.md` |
| Session workflows and the menu | `protocols/hi-menu.md`, `.claude/commands/` |
| Agent roles and their archetypes | `.claude/agents/`, `protocols/atelier.md` |
| On-demand contract index | `protocols/README.md` |
| Retrieval and quality gates | `scripts/semantic.py`, `scripts/lint.py`, `scripts/privacy_check.py` |

The file boundaries are:

- `CLAUDE.md` holds shared runtime invariants; `AGENTS.md` adapts them for Codex.
- `harness/` owns registration and runtime/model metadata, not workflow bodies.
- `.claude/commands/` and `.claude/agents/` are the current shared workflow and role sources. Their Claude-shaped location is a compatibility boundary, not a second source for Codex.
- `protocols/` holds shared contracts; `frameworks/` and `sources/` are on-demand references, not required startup context.
- `scripts/` owns executable behavior; [its map](scripts/README.md) separates shared infrastructure, applications, and verification. `tests/` owns the checks.

Generated runtime edges (`.codex/agents/`, `.agents/skills/`, except the hand-written `atelier` skill) are rendered by `scripts/render_runtime_edges.py`. Edit their registry inputs, never generated files. Runtime hooks remain hand-maintained. Private knowledge and preferences remain outside this public tree.

## Forking

MIT, for the code. Expect rip-and-replace, not clone-and-run: `profile/`, vault content, the impressionist vocabulary (*le cercle*, *the Painter*, *the œuvre*), the bilingual English/Chinese behavior, and the `civ` / `dine` / `prm` life-area workflows are bespoke and deliberately non-portable. The value of a system like this lives in writing your own taxonomy. Take the patterns; build your own atelier.
