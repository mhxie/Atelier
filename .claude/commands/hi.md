---
description: Universal entry point — intent router for reflection, planning, action, reading, learning, capture, and more.
---

Outcome: select one intent, load only its bounded context and procedure, and
execute it through the active runtime.
Done when: the route is visible, real ambiguity is resolved, and the selected
procedure's completion criteria are met.
Evidence: the catalog row selected, bounded context metadata when used, and
the procedure's own output evidence.
Output: one routing announcement followed by the selected workflow.

## No-context invocation

When `/hi` or `$hi` has no following text, do not load the catalog. Read
`protocols/hi-menu.md`, ask its two-stage menu, then load only the chosen
procedure. Its recommendation branch may dispatch Librarian.

## Contextual routing

Run `uv run scripts/intent_coverage.py catalog --examples` and read it: one
line per `harness/intents.toml` row (plus any private rows from the local
overlay, marked `(private)`) with its `description`, example phrases, and
dispatch tag. Pick the row whose description fits the request's primary
intent; examples illustrate a row, they are not triggers. Judge the whole
message, not a keyword inside it: a URL inside a day's narrative is capture,
not reading; a question that cites a link is a question. A date-prefixed
factual narrative with no analytical question is `capture`. Requests that fit
no row are `general`, which hands off to the runtime's ordinary skill, app,
agent, or tool routing. Never choose `reflection` because nothing else fit.

Clarify only when two rows fit equally well and the choice changes what runs,
or when a low-confidence route would open a file, call an external service,
write, or start a multi-agent chain. Offer the plausible rows plus `general`.
Do not clarify a request whose reading is obvious in context.

Log every contextual route once, best-effort, before dispatch:

```bash
uv run scripts/intent_coverage.py intent-log --quiet \
  --input "<raw hi text>" --runtime <claude-code|codex> \
  --match-kind <routed|general|clarified> --intent <name> \
  [--candidates a,b --clarified-to <name>] [--final-dispatch <label>]
```

`routed` is a confident pick, `general` a handoff (add `--final-dispatch
<capability>` once you know what ran), `clarified` a route the user chose
from candidates. If the user redirects a confident route after the
announcement, log a second line before continuing: `--match-kind corrected
--intent <announced> --clarified-to <actual>`. That line is the only record
of a false hit. The ledger is the coverage signal for
`protocols/intent-coverage.md`; a failed write never blocks dispatch.

## Load and dispatch

Announce `Routing as intents.<name> → <agents>`, adding `(parallel)` when the
catalog tag says so. For `general`, say `No Atelier intent matched; handing
off semantically → <capability>` and execute `intents.general`.

When the row declares `profile_reads`, run:

```bash
uv run scripts/context_bundle.py --intent <name> --format json
```

The helper applies the row's `context_budget_bytes`. Reuse the projection; do
not reload the same profile or continuity sources. Skip it when
`profile_reads` is empty unless the procedure requests a specific source.

Read only the row's `procedure` file and execute it as the selected workflow.
When the row is parallel, dispatch the declared initial agents in one native
batch. Procedure-level parallel steps follow the same rule. All writes retain
the approvals and daily-note boundaries from `CLAUDE.md`.
