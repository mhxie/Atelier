# Harness

Provider-neutral registries and maintainer reference for the Atelier runtime
layer.

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

## Validation

Install the locked dependencies using the root [getting-started guide](../README.md#get-started).
After a registry edit, render the Codex edges as described above. Before
finishing harness changes, run:

```bash
python3 scripts/harness_lint.py
.venv/bin/python scripts/harness_smoke.py
python3 scripts/harness_lint.py --footprint
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

Smoke runs harness lint, unittest discovery (including runtime mappings and
hooks), and offline Ruff when available, without reading the private vault.
Inspect skipped checks: missing Node dependencies or Ruff reduce coverage.
It does not verify live model calls, connectors, scheduled deployment, or
publication privacy. Those require the selected workflow's own checks; see the
[publication gate](../scripts/README.md#public-repo-privacy-gate) for that boundary.

## Roadmap

Planned, in this order. These are not implemented capabilities or release
commitments; each stage needs an approved implementation plan and verification.

1. **Confirmed action handoff.** Extend the [meeting procedure](../protocols/intent-meeting.md)
   and existing [Planner/Executor contract](../protocols/local-first-architecture.md#planner-vs-executor-orthogonal-to-l1-l5)
   so selected, approved actions reach an identified task record with source
   links and result backfill. Saving a meeting note must not accept every
   proposed task. Verify subset approval, duplicate prevention, unchanged
   raw-capture authority, and evidence-backed closure.
2. **Verified external follow-through.** Extend the selected workflow or private
   feature to check actual mail/calendar results and track approved follow-ups
   in existing task records. Verify external authority separately from record
   writes, preserve real deadlines alongside follow-up dates, and leave
   uncertain outcomes open without blind retries. A sent request is not a reply.
3. **Decision and research revisits.** Connect existing [decision review triggers](../.claude/commands/decision.md)
   to the [weekly review](../.claude/commands/weekly.md); connect configured research
   verification and challenge outputs back to the original question or decision.
   Add routine reminders through existing cues and brief aggregation only when
   needed. Verify due reviews, evidence-backed event triggers, and retirement
   of superseded reminders; a saved report is not a verified conclusion.

Keep one owner per fact and extend existing workflows first. This roadmap does
not propose another task database or scheduler. Directory reorganization is
separate work, justified by real module boundaries. Implementation must fit
the [whole-feature budget and approval rules](../protocols/repo-conventions.md#editing-discipline).
