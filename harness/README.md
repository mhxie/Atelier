# Harness

Provider-neutral registry files for the Atelier runtime layer.

| File | Purpose |
|---|---|
| `commands.toml` | Portable workflow names mapped to `.claude/commands/*.md` sources; Codex exposes matching `$command` skills. |
| `agents.toml` | Portable role names mapped to source files (typically `.claude/agents/*.md`; a script for script-driven roles) and a per-role `voices = {leg = "model", ...}` table. Allowed leg keys: `native`, `direct`, `codex`. |
| `intents.toml` | Intent catalog for `/hi`: one-line `description` per row (the model classifies against it) mapped to one procedure path, Repomix token ceiling, dispatch shape, and profile reads. |
| `models.toml` | Model identity registry with runtime-neutral reasoning tiers (identity names like opus, sonnet, deepseek_pro_max; no provider bindings). Provider/model bindings live in gitignored `profile/models.toml` and merge at runtime. |
| `capabilities.toml` | Runtime-neutral capability names and the Codex-side tool that implements each. The Claude Code mapping lives in `.claude/agents/*.md` `tools:` frontmatter (single source of truth). |
| `retrieval.toml` | Local QMD models and hardware resource presets; `semantic.toml` carries gitignored machine overrides. |
| `runtimes.toml` | Native CLI registry and shipped Codex default; each runtime also declares its surface (instruction file, agent dir/format, skills dir, hooks file, supported primitives) for the edge renderer. A user can persist Claude in gitignored `runtime.local.toml`. |
| `runtime.local.toml.example` | Template for the optional per-user runtime default. `scripts/atelier_runtime.py use <runtime>` writes the gitignored live file. |
| `paths.toml` | Canonical logical-name → vault-path registry for L1–L4 surfaces and the private operational feature root (the `<paths.<name>>` placeholders in docs). Renames happen here; per-user extensions live in gitignored `paths.local.toml`. |
| `paths.local.toml.example` | Template for the gitignored per-user `paths.local.toml` (localized wikis, sandbox overrides, private tiers). |
| `registry.schema.json` | Structural rules for the seven public registries and both native hook configurations. |

Runtime entry surfaces:

```text
Claude: /hi, /weekly, /review
Codex:  $hi, $weekly, $review
```

The optional selector preserves those native forms while sharing one default:

```bash
python3 scripts/atelier_runtime.py run hi
python3 scripts/atelier_runtime.py use claude
python3 scripts/atelier_runtime.py run hi
```

Command skills live under `.agents/skills/`; native Codex roles live under
`.codex/agents/`. Both point directly to the shared source files declared in
the registries, and both are generated:
`uv run scripts/render_runtime_edges.py --runtime codex --check` proves the
committed edge matches the registries byte-for-byte (the smoke suite runs it);
use `--apply` after editing a registry. `.agents/skills/atelier/` and
`.codex/hooks.json` remain hand-maintained.

Before finishing harness changes:

```bash
python3 scripts/harness_lint.py
python3 scripts/harness_smoke.py
```

The lint first validates one public-config snapshot with pinned
`check-jsonschema`, then checks references, paths, native edges, and shared
contracts. It uses the project `.venv` when present; bootstrap dependencies with
`uv sync --locked`. Lint never installs dependencies or fetches schemas: the
committed schema has only internal references. Private overlays retain their
separate checks and never enter the schema batch. `--footprint` needs no validator.

The JSON report and exit codes are unchanged. Structural errors use
`registry-schema` with the source filename and JSON instance path; unreadable or
malformed config uses `registry-read`; validator/setup failures use
`registry-validator`. Fix these before cross-file checks can run.

The smoke test exercises runtime selection, native skill and agent mappings,
and hook behavior without reading the private vault.
