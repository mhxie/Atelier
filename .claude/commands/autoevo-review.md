---
description: Triage the autoevo pending queue produced by /autoevo-nightly.
---
# /autoevo-review — Triage the autoevo pending queue

Morning companion to `/autoevo-nightly`. Walks `$OV/_meta/autoevo_pending.toml` one pending entry at a time: apply, skip, defer, or explain-more. Contract: `protocols/autoevo.md`.

## When to run

The cue at `/hi` fires when `$OV/_meta/autoevo_pending.toml` has entries with `status = "pending"`. Run this command to triage them. Snoozable per the standard cue mechanism (`uv run scripts/cues.py snooze autoevo_pending --days N`).

## Step 1: Load the queue

```bash
# Bind run identity for the rest of the session. RUN_DATE is referenced by
# step 2 (auto-dismiss commit), step 5 (closing summary), and step 6 (final
# commit body). Binding it once here keeps later steps composable.
RUN_DATE=$(date +%Y-%m-%d)
QUEUE="$OV/_meta/autoevo_pending.toml"
if [ ! -f "$QUEUE" ]; then
  echo "no pending queue at $QUEUE — nothing to review"
  exit 0
fi
```

Parse with `tomllib`. Filter to entries where `status == "pending"` (skip `applied`, `dismissed`, `auto-dismissed`).

If filtered set is empty: print "queue empty (or fully resolved)" and exit 0.

## Step 2: Auto-dismiss past-due entries (housekeeping)

Before showing anything to the user, sweep for entries that triggered the auto-dismiss rule per `protocols/autoevo.md`:

- `surface_count >= 3` (the helper's built-in threshold), OR
- `proposed_at` is older than the helper's `--max-age-days` (default 30).

Run `uv run --quiet python3 scripts/autoevo_pending.py auto-dismiss --today <RUN_DATE>`; it sets `status = "auto-dismissed"` with a `dismiss_reason` and prints the affected ids. For each, append a one-line note to `<paths.agent_findings>/autoevo-applied-<RUN_DATE>.md` § "Auto-dismissed". Resolve the registry-backed path before committing so a future rename of the `agent_findings` segment in `harness/paths.toml` does not silently break the git add:

```bash
PATHS_FINDINGS=$(uv run --quiet python3 -c "from scripts._paths import tier; print(tier('agent_findings'))")
FINDINGS_REL="${PATHS_FINDINGS#$OV/}"   # portable shell strip; macOS realpath has no --relative-to
uv run --quiet python3 scripts/autoevo_commit.py queue \
  --summary "auto-dismiss <N> stale pending entries" \
  --detail "Categories: <breakdown>. Reason: surface_count >= 3 OR proposed_at older than --max-age-days" \
  --extra-path "${FINDINGS_REL}/autoevo-applied-${RUN_DATE}.md"
```

`scripts/autoevo_commit.py` is the sole committer: it stages the queue file plus `--extra-path`, commits `--only` those paths, and applies the bot author/committer identity.

If no auto-dismiss candidates, skip the commit and continue.

## Step 3: Group and present

Group the remaining pending entries by category (`redundant`, `time-stale-A`, `time-stale-B`, `contradicted`, `low-signal`). Print a one-screen summary first, like:

```
Pending autoevo decisions (N total):

redundant: 3
  [a] merge 3 notes in wip/foo-* (scores 0.78/0.72/0.61)
  [b] merge 2 notes in wip/bar-* (scores 0.71/0.68)
  [c] merge 4 notes in research/baz/* (scores 0.74-0.81)

time-stale-A: 1
  [d] wip/old-plan.md — content-stale ("by end of Q3 2025"); default stale-banner on 2026-06-05 unless skipped

contradicted: 1 (Challenger confirmed)
  [e] wiki/Decision X.md [C2] vs reflections/2026-04-15.md

low-signal: 2
  [f] wip/note-x.md (87 words, 0 links, 90-365d cold)
  [g] research/area/note-y.md (62 words, 0 links, 90-365d cold)

Pick category to triage, or skip:
  [r]edundant | [t]ime-stale | [c]ontradicted | [l]ow-signal | [a]ll | [q]uit
```

Entries carrying `default_action` / `default_at` (protocols/autoevo.md § Default
after a veto window) show their deadline. The veto is whichever action
contradicts the default: skip vetoes a `stale-banner` default, apply vetoes a
`dismiss` default. Skipping a `dismiss` default agrees with it, and the ledger
records it as a confirmation, so offer apply as the veto on those entries.
Defer restarts the 14-day window; silence lets the nightly apply the default.

Wait for user input. Default if unclear: walk all in order.

## Step 4: Per-item triage loop

For each entry in the chosen category (or all if `[a]`), present and prompt:

```
[<id>] <category>
Proposed: <proposed_action>
Evidence: <evidence_summary>
Peers: <paths>
Proposed: <proposed_at>  Surfaced: <surface_count>x  Last: <last_surfaced>

Action? [a]pply | [s]kip | [d]efer | [e]xplain | [q]uit
```

Apply and skip both take one sentence of reason; it is mandatory and becomes
a precedent in `$OV/_meta/decisions.jsonl` (`protocols/decision-ledger.md`).
Offer these chips first, free text otherwise: `still active, not stale`,
`intentionally separate`, `wrong peer / misread`, `agree, do it`,
`superseded elsewhere`. Never accept an empty reason and never invent one.

### Apply

Dispatch the relevant agent in normal (approval-mode) Curator or Challenger flow. The user reviews the agent's proposal as usual — `/autoevo-review` does NOT shortcut Curator's content-preservation gates. Then:

- On user-confirmed write: `uv run --quiet python3 scripts/autoevo_pending.py resolve --id <id> --status applied --reason "<user's sentence>" --today <RUN_DATE>`, append to audit log § "Applied via /autoevo-review", commit.
- On user-rejected proposal: `uv run --quiet python3 scripts/autoevo_pending.py resolve --id <id> --status dismissed --reason "<user's sentence>" --today <RUN_DATE>`, commit.

### Skip

Run `uv run --quiet python3 scripts/autoevo_pending.py resolve --id <id> --status dismissed --reason "<user's sentence>" --today <RUN_DATE>`, commit. Move to next item. (Never hand-edit the TOML: the helper sets `resolved_at`, which anchors the nightly dedupe window.) For an entry whose default is `stale-banner` this is the veto; say so in one line. For an entry whose default is `dismiss`, skip AGREES with the default and is recorded as a confirmation, not a veto: say that instead, and name apply as the veto.

### Defer

Run `uv run --quiet python3 scripts/autoevo_pending.py defer --id <id> --today <RUN_DATE> [--reason "<sentence>"]` (increments `surface_count`, sets `last_surfaced`, and pushes `default_at` to today + 14 when the entry carries a default; a reason is optional here and recorded when given). Optional snooze: ask "snooze for how many days? (default 7)". On a snooze ≥ 1, also call:

```bash
uv run scripts/cues.py snooze autoevo_pending --days <N>
```

This snoozes the entire autoevo_pending cue group; per-entry snoozing is not supported (keeps cue surface simple). Move to next item.

Commit the queue update at the end of the session (one commit covers all defers), not per-item, to avoid commit-spam:

```bash
uv run --quiet python3 scripts/autoevo_commit.py queue \
  --summary "defer <N> pending entries in <RUN_DATE> triage" \
  --detail "Categories: <breakdown>"
```

### Explain

Print the entry's full evidence dict (peer paths with their content snippets, retrieval scores per-peer, mode/floor for redundant, dated phrases for time-stale, contradiction signal text for contradicted). Then re-prompt with `[a/s/d/q]`.

### Quit

Stop the loop. Whatever was applied / dismissed / deferred so far is preserved (commits already happened). Remaining entries stay `pending`. Next /hi cue will surface them again.

## Step 5: Closing summary

When the loop ends (user picked `q` or walked the whole list), print:

```
/autoevo-review session summary (<duration>):

Applied:    <N>
Dismissed:  <N>
Deferred:   <N>
Remaining:  <N>

Queue file: $OV/_meta/autoevo_pending.toml
Audit log:  <paths.agent_findings>/autoevo-applied-<RUN_DATE>.md
```

If anything was applied, append a "Session via /autoevo-review" subsection to the day's audit log with per-item SHA references.

## Step 6: Final queue commit (if needed)

If steps 4-5 produced any queue mutations not already committed (the defer batch, status changes), commit now:

```bash
uv run --quiet python3 scripts/autoevo_commit.py queue \
  --summary "triage session <RUN_DATE> resolved <N> entries" \
  --detail "Applied=<n>, Dismissed=<n>, Deferred=<n>"
```

(If everything was applied and the queue is empty, the file still gets committed in its empty/header-only state — easier to revert than to special-case.)

## Edge cases

- **Queue file is corrupted.** Refuse to operate. Surface to user: "queue parse failed: <error>. Repair `$OV/_meta/autoevo_pending.toml` manually then re-run." Do not auto-rewrite.
- **An entry's peers no longer exist** (user already manually deleted them between the nightly run and triage). Skip the entry via `autoevo_pending.py resolve --id <id> --status dismissed --reason "peers no longer present"`. Log to audit.
- **Wiki entry path requested for compaction** (a contradicted finding's `wiki_path`). Triage routes the Apply action to Curator's normal wiki-update flow (full approval gate, Revision Log append). `/autoevo-review` does NOT shortcut the wiki update path.
- **Daily note appears anywhere in a pending entry.** Reject the entry on load — write to audit log § "Errors" with the entry id and run `uv run --quiet python3 scripts/autoevo_pending.py resolve --id <id> --status dismissed --reason "daily note in pending entry; nightly bug" --today <RUN_DATE>`. The nightly run should never have queued a daily-note finding; if it did, treat as a bug to fix in Forgetter.

## What this command does NOT do

- Does not push to `origin`.
- Does not modify wiki entries without going through Curator's normal approval gate.
- Does not delete files unilaterally. Deletions go through Curator; archives go through `git mv` per `/autoevo-nightly` step 4b.
- Does not bypass Forgetter's contradicted → Challenger flow. Contradicted entries in the queue were already Challenger-confirmed during the nightly run.

## Related

- `protocols/autoevo.md` — contract.
- `.claude/commands/autoevo-nightly.md` — producer of the queue.
- `.claude/agents/curator.md` — op executor for Apply.
- `.claude/agents/challenger.md` — for re-probing if user asks during Explain.
- `scripts/cues.py` § `check_autoevo_pending` — the cue surface.
