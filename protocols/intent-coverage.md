# Intent Coverage

Feedback loop for the shared `/hi` (Claude Code) and `$hi` (Codex) router.
Routing is model judgment over the `description` of each `harness/intents.toml`
row (`scripts/intent_coverage.py catalog`); this protocol defines the ledger
that records every route and the review that turns recurring unrouted
requests into catalog work.

## Route ledger

After routing and before dispatch, the orchestrator appends one line per
contextual invocation with `scripts/intent_coverage.py intent-log`. Kinds:

| Kind | Meaning |
|---|---|
| `routed` | One row fit with confidence; `intent` names it. |
| `general` | Nothing fit; `intents.general` handed the request to the runtime's ordinary routing. `final_dispatch` may name what ran. |
| `clarified` | The orchestrator asked the user to choose; `candidates` lists what was offered and `clarified_to` what was picked. |
| `corrected` | A confident route the user redirected after the announcement: `intent` is the announced row, `clarified_to` the one that should have run. Logged as a second line after the original `routed` line; this is the false-hit signal, and `corrected / routed` is the false-hit rate in `scripts/eval_run.py`. |

Location: `$OV/_meta/intent_routes/YYYY-MM-DD.jsonl`, falling back to
`~/.cache/atelier/intent_routes/` when `$OV` is unset. The filename date is
wall-clock `date.today()` at write time, not the late-sleep effective date:
this is an audit trail, not a user-authored surface. The legacy
`$OV/_meta/intent_misses/` log written by the retired substring router is
still read; every line there counts as a miss.

Schema:

```json
{
  "timestamp": "2026-09-02T10:12:03",
  "runtime": "claude-code",
  "raw_input": "improve the repo so that ...",
  "match_kind": "general",
  "intent": "general",
  "candidates": ["reading", "explore"],
  "clarified_to": "reading",
  "final_dispatch": "engineering-task",
  "notes": "free text"
}
```

`candidates`, `clarified_to`, `final_dispatch`, and `notes` are optional. The
write is best-effort: empty input is skipped with a stderr note, an OSError is
swallowed, and the command always exits 0 so a slow or unmounted `$OV` never
blocks a live invocation. Entries stay under the POSIX append atomicity bound;
the reader drops any torn line.

## Review

```
uv run scripts/intent_coverage.py intent-misses [--since YYYY-MM-DD] [--match-kind <kind>] [--runtime claude-code|codex] [--top N] [--propose] [--json]
```

A miss is any event whose kind is not `routed`. The report prints counts by
kind, the top unrouted phrases (NFKC-normalized, casefolded, 200 chars), and
the coverage signal: phrases recurring on at least
`INTENT_MISS_DISTINCT_DAYS_THRESHOLD` (3) distinct file dates. `--propose`
lists those repeaters with their clarified or dispatched target; `--json`
carries the same rows under `proposals` for `/triage`. `--since` filters at
file-date granularity.

`scripts/eval_run.py` records route coverage (confident routes over all
routes in the last 30 days) as the `routing` component of each eval snapshot,
and `scripts/cues.py check_intent_misses` raises a soft cue at 5+ unrouted
requests in 14 days. Coverage is not correctness; see the judged eval below.

## Judged routing eval

Coverage says how often a route was confident, not whether it was right.
Correctness is checked by a cheap model acting as the classifier: dispatch a
`general-purpose` subagent (model `sonnet`) with the prompt below, then fold
its verdict into the eval snapshot. Run it after any change to a
`description`, before `/system-review` on a routing change, or when the
coverage cue fires. It costs about 45k subagent tokens and a minute; nothing
enters the main context except the verdict.

Two case sets feed it: the public fixture, and the private regression set at
`$OV/_meta/evals/routing_cases.json` (`{"cases": [{"id", "input", "label"?}]}`,
seeded from the retired substring router's real misses). Cases with a
`label` score accuracy; cases without one report the pick distribution and
the clarify rate. Append new `corrected` and `clarified` ledger phrases to it
when they recur.

Prompt (verbatim, fill the two paths):

```text
You are the /hi intent classifier for the repo at <repo root>. Judged routing
eval: classify each fixture input against the intent catalog using ONLY the
row descriptions.
1. Run exactly `uv run scripts/intent_coverage.py catalog`. Do NOT pass
   --examples, do NOT open harness/intents.toml, and do NOT open anything
   under tests/ except the fixture below; examples would leak answers.
2. Read `.claude/commands/hi.md` § Contextual routing.
3. Read `tests/fixtures/routing_evalset.json` (cases: [{input, expected}]).
4. For each case write your pick BEFORE reading its `expected`; use `general`
   when no row fits. Note whether hi.md's clarify rule would have fired.
5. Write ONLY this JSON to <verdict path>: {"model": "sonnet", "cases": N,
   "passed": N, "catalog_bytes": N, "misses": [{"input": ..., "expected":
   ..., "got": ..., "why": one sentence, "suggest": reworded description}],
   "collisions": [{"rows": ["a", "b"], "why": ...}]}
Do not modify any other file.
```

Then:

```bash
uv run scripts/eval_run.py --no-semantic --judged-routing <verdict path>
```

The snapshot records `judged.routing` (`cases`, `passed`, `score`, `misses`,
`model`), and `scripts/cues.py check_eval_regression` compares it across
consecutive snapshots alongside route coverage. Act on `misses` and
`collisions` exactly as on a recurring phrase below: the fix is always a
sharper description, never priority machinery.

## Acting on a recurring phrase

1. **Sharpen a description.** The request belongs to an existing row whose
   `description` did not make that obvious. Edit the description; it is the
   whole routing contract. Adding the phrase to `examples` (canonical, or the
   gitignored `harness/intents.local.toml` overlay for private phrasing) is
   secondary and informational.
2. **Add a row.** The request is a workflow `/hi` does not model yet. Write
   the procedure first, then the row; decide whether it also deserves a direct
   command in `harness/commands.toml`.
3. **Add a private row.** The request is a private feature or private
   command the public catalog cannot name. In `harness/intents.local.toml`:

   ```toml
   [intents.my-feature]
   description = "One line the classifier routes on."
   procedure = "my-feature/SKILL.md"   # absolute, $OV-relative, or under <paths.private_features>
   examples = ["optional phrasing"]
   ```

   The row appears in the catalog marked `(private)` with the defaults of a
   solo, script-free route (`mode = "private-feature"`, no profile reads);
   `mode`, `agents`, `profile_reads`, `context_budget_bytes` may be set.
   Requests for private capabilities that reach `general` are the largest
   source of false hits into neighbouring public rows; this is the fix.
4. **Accept the miss.** One-off engineering, app, or tool requests belong to
   the general handoff. The recurring count is the audit trail; no edit.

Descriptions must stay disjoint. When two rows attract the same phrase, the
fix is to narrow one description, never to add priority machinery.

## Related

- `harness/intents.toml`: the catalog; `description` is the routing contract.
- `.claude/commands/hi.md` § Contextual routing: when to clarify, what to log.
- `scripts/intent_coverage.py`: `catalog`, `intent-log`, `intent-misses`.
- `protocols/shadow-log.md`: sibling JSONL-append and report system.
