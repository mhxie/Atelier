# Atelier

> **A personal workshop, published.** A reflective-thinking system for [Codex CLI](https://github.com/openai/codex), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), and a local-first Zettelkasten: daily reflection, decision journals, deep reading, goal tracking, knowledge crystallization. Not a product. The patterns are reusable; the configuration is bespoke.

The system surrounds an **œuvre**: notes, decisions, and reflections kept as local Markdown under `$OV/`, outside this repository. Fifteen agents (le cercle) run the sessions, a deterministic trust engine scores the wiki layer, and one registry layer drives both runtimes. The repo is written to be read in place, by people and by agents; this page is only the map.

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
| The behavior index agents start from | `protocols/README.md` |
| Rules every harness change passes | `protocols/evolution.md` |
| Retrieval and quality gates | `scripts/semantic.py`, `scripts/lint.py`, `scripts/privacy_check.py` |

Generated runtime edges (`.codex/agents/`, `.agents/skills/`) are rendered from the registries by `scripts/render_runtime_edges.py`; edit registries, never the generated files.

## Forking

MIT, for the code. Expect rip-and-replace, not clone-and-run: `profile/`, vault content, the impressionist vocabulary (*le cercle*, *the Painter*, *the œuvre*), the bilingual English/Chinese behavior, and the `civ` / `dine` / `prm` life-area workflows are bespoke and deliberately non-portable. The value of a system like this lives in writing your own taxonomy. Take the patterns; build your own atelier.
