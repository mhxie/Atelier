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
| `corrected` | A confident route the user redirected after the announcement: `intent` is the announced row, `clarified_to` the one that should have run. Logged as a second line after the original `routed` line; this is the false-hit signal. |

Location: `$OV/_meta/intent_routes/YYYY-MM-DD.jsonl`, falling back to
`~/.cache/atelier/intent_routes/` when `$OV` is unset. The filename date is
wall-clock `date.today()` at write time, not the late-sleep effective date:
this is an audit trail, not a user-authored surface.

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

`scripts/cues.py check_intent_misses` raises a soft cue when a phrase recurs
unrouted on 3+ distinct days within 14 days. A confident route is not
necessarily a correct one; the ledger records what happened, not a verdict.

## Acting on a recurring phrase

1. **Sharpen a description.** The request belongs to an existing row whose
   `description` did not make that obvious. Edit the description; it is the
   whole routing contract. Adding the phrase to `examples` (canonical, or the
   gitignored `harness/intents.local.toml` overlay for private phrasing) is
   secondary and informational.
2. **Add a row.** The request is a workflow `/hi` does not model yet. Write
   the procedure first, then the row; decide whether it also deserves a direct
   skill in `harness/skills.toml`.
3. **Add a private row.** The request is a private component the public
   catalog cannot name. In `harness/intents.local.toml`:

   ```toml
   [intents.my-skill]
   description = "One line the classifier routes on."
   procedure = "my-skill/SKILL.md"   # absolute, $OV-relative, or under <paths.private_skills>
   examples = ["optional phrasing"]
   ```

   The row appears in the catalog marked `(private)` with the defaults of a
   solo, script-free route (`mode = "private"`, no profile reads);
   `mode`, `agents`, `profile_reads`, `context_budget_tokens` may be set.
   Requests for private capabilities that reach `general` are the largest
   source of false hits into neighbouring public rows; this is the fix.
4. **Accept the miss.** One-off engineering, app, or tool requests belong to
   the general handoff. The recurring count is the audit trail; no edit.

Descriptions must stay disjoint. When two rows attract the same phrase, the
fix is to narrow one description, never to add priority machinery.

## Related

- `harness/intents.toml`: the catalog; `description` is the routing contract.
- `skills/hi/SKILL.md` § Contextual routing: when to clarify, what to log.
- `scripts/intent_coverage.py`: `catalog`, `intent-log`, `intent-misses`.
