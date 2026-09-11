# scripts/

Local tooling, grouped by responsibility below. Start with the selected
workflow and its named entry point; use that script's `--help` and module
docstring for supported operations, dependencies, and failure behavior.
This map is not a second per-script specification.

## Entry Points

| Responsibility | Start here |
|---|---|
| Runtime selection, capability evidence, generated edges | `atelier_runtime.py`, `runtime/capabilities.py`, `render_runtime_edges.py`; `harness_lint.py` checks declarations; ownership in `harness/README.md` and `sources/runtimes/README.md` |
| Route context and session evidence | `intent_coverage.py`, `context_bundle.py` (strict selection + Repomix), `session_log.py` |
| Retrieval and knowledge validation | `semantic.py` calls the pinned QMD SDK through `qmd.mjs`; `trust.py` and `lint.py` validate knowledge under the [wiki schema](../protocols/wiki-schema.md) |
| Reading feedback and decisions | `decisions.py` (including `reading-*` commands), `precedent.py`; `reading_feedback.py` owns pure reading-event validation and evaluation |
| Hosted-model calls | `chat_completion.py`, bound through `harness/models.toml`; `precedent.py` is its caller |
| Scheduled execution and recovery | `routine_prefect.py` owns Prefect flows/deployments; `routine_adapter.py` owns fixed execution, receipt writes, and replay decisions; `routine_receipts.py` owns shared ordinary artifact validation; `routine_status.py` reads native state. Contracts: `../protocols/remote-routines.md`; operations: `launchd/README.md` |
| Knowledge maintenance | `decay_scan.py`, `autoevo_run.py`; queue, preflight, commit, and verification helpers preserve separate safety boundaries |
| Daily brief and optional life-area applications | `routine_digest.py`, `daily_brief.py`, `dining_audit.py`, `dine_rank.py`, `interests.py` |
| Public-repo privacy | `privacy_check.py`, `privacy_index.py`, `hooks/pre-push`; approval contract below |
| Verification | `../tests/` owns unittest scenarios; `harness_smoke.py` runs lint, discovery, and Ruff |

Supporting files stay with their owning subsystem. A script need not become a
registered command or agent. Domain applications are not universal harness
requirements; load them only through the selected workflow. Moving one to a
new directory does not reduce its implementation cost.

`invocation_log_gc.py` is a legacy privacy-retention owner: lifecycle hooks
use it to age out legacy full-payload API logs while no new logs are written.

## Portable Harness

Claude commands under `.claude/commands/` and Codex skills under
`.agents/skills/` are the native runtime edges. Run `scripts/harness_smoke.py`
after harness edits to verify those mappings and lifecycle hooks without
touching `$OV/`.

`scripts/atelier_runtime.py` is optional for direct interactive use. It ships
with Codex selected, launches `$<skill>` or `/<skill>` unchanged, and lets
the user persist Claude with `python3 scripts/atelier_runtime.py use claude`.

## Public-repo privacy gate

Keep identity, goals, locations, account labels, schedules, and preference
policy in the private vault outside the clone or gitignored `profile/`. Add
exact literals that cannot be inferred from vault filenames to
`profile/private_terms.txt`, one per line;
single-word compatibility entries may remain in `profile/private_slugs.txt`.
Neither file is committed.

Before a public-bound commit, inspect the exact diff and run
`uv run scripts/privacy_check.py --json` with the intended private vault available.
The scanner checks tracked and untracked public-bound paths and files plus any
divergent staged blobs. Its JSON reports a coverage warning when the local
exact-term sidecar is absent; a clean hit count does not erase that warning.
A skipped scan is not privacy clearance. Before committing, also obtain the
required semantic review of the proposed changes; the scanner does not grant
commit authority. Only when publishing existing, authorized local commits,
invoke `$push` (Claude: `/push`). The [publish procedure](../skills/push/SKILL.md)
owns mechanical and independent semantic review of the same unpushed history,
including intermediate commits, and then performs the push. It does not commit
working-tree changes. Missing semantic coverage must not be reported as clean.

The optional `scripts/hooks/pre-push` hook repeats only the mechanical history
scan; it does not replace semantic review. Install it explicitly per clone with
`git config core.hooksPath scripts/hooks`. These gates do not erase sensitive
content from previously published history or remote forks.

The project's code is licensed under [MIT](../LICENSE). Preserve the copyright
and license notices when redistributing it.

## Conventions

- **Exit-code convention for JSON-emitting scripts**: `0` success, `1` a
  reported failure the caller can act on (a refused op, a verdict of "not
  verified"), `2` usage, input, or data errors and any unforeseen exception,
  always with a single `{"error": "..."}` object on stdout. `harness_smoke.py`
  and the routine runner branch on these.
- **Shared helpers**: `_paths.py` (registry paths, `atomic_write`,
  `parse_iso_date`, `retry_transient`) and `_git.py` (git subprocess). New
  scripts import these instead of re-implementing them.
