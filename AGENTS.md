# AGENTS.md: Atelier Codex adapter

Codex reads this file; Claude Code reads `CLAUDE.md`. Read `CLAUDE.md` once at
session start for the shared safety, knowledge, and writing contract. Load
`protocols/runtime-adapters.md` only when changing or debugging portability.

## Runtime edge

- Shared behavior lives in `CLAUDE.md`, `protocols/`, canonical skills and agents, registries,
  and scripts. Runtime syntax stays in `.claude/`, `.codex/`, and this adapter.
- User skills are Claude `/name` and Codex `$name`. Both runtime edges read
  `skills/<name>/SKILL.md`; never start nested Codex.
- `$hi` classifies against the `harness/intents.toml` catalog, then reads
  only the selected `procedure`. Direct skills skip the universal router.
- Native roles use `.codex/agents/<role>.toml`, which points to the canonical
  `agents/<role>.md` brief. If dispatch is unavailable, run that brief
  sequentially and disclose the downgrade.
- Private skills live under `<paths.private_skills>/` and may be linked into
  user-level skill discovery. Deterministic private tools and routines live
  under their own registered roots and never enter skill discovery. Never
  commit private names to public registries.

| Claude construct | Codex adaptation |
|---|---|
| `Read` | Read the named local file or bounded section. |
| `Grep` / `Glob` | Use `rg` / `rg --files` with scoped paths. |
| `Bash` | Use the shell in the project workspace. |
| `Write` / `Edit` | Use the local patch or write tool after required approval. |
| `AskUserQuestion` | Use the native choice UI or a concise numbered question. |
| `Agent(role)` | Dispatch the matching project agent or emulate its brief. |
| `WebSearch` / `WebFetch` | Use enabled web tools or report the limitation. |

Project hooks live in `.codex/hooks.json`. Trust permits loading project
configuration; it does not bypass approvals or the sandbox.

## Harness changes

Keep workflows provider-neutral and runtime adapters thin. Update the relevant
`harness/*.toml` registry and `.agents/skills/atelier/SKILL.md` when behavior
changes. Claude and Codex runtime edges (except `$atelier`) are rendered:
after a registry edit run
`uv run scripts/render_runtime_edges.py --runtime all --apply` instead of
hand-editing them. Run `python3 scripts/harness_lint.py` and
`.venv/bin/python scripts/harness_smoke.py` before finishing.
