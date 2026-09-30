---
name: review
description: Goal review for quarterly full reviews and monthly light pulses.
---
# Goal Review

> Also reachable via `/hi <natural language>` (e.g., `/hi review my goals`,
> `/hi goal pulse`, `/hi check directions`). See `harness/intents.toml`
> `[intents.review]` for the row's example phrases. Both paths execute this same procedure.

Review progress on near/mid/long-term goals. Surface what's progressing, what's neglected, and what has shifted.

## Cadence

| Type | Frequency | What |
|---|---|---|
| **Full review** | Quarterly (start of Q1 / Q2 / Q3 / Q4, i.e., early Jan / Apr / Jul / Oct) | Walk every goal in `directions.md`, decide keep / shift / drop. Retire >1y stale goals. Trend table vs. prior reviews. |
| **Light pulse** | Monthly | 5-min check inside `/weekly` or standalone: any goal materially advanced? Any neglected? Any newly born? No file write required if nothing surfaced. |
| **Annual reset** | Yearly (Jan or birthday) | Full rebuild anchored to `<year>小目标`. Touches `directions.md` directly. Often paired with `/introspect`. |

Inspect the last full review and the last pulse-equivalent so the monthly cadence does not produce duplicate pulses within 30 days. A pulse-equivalent is either a standalone `*-review-pulse.md` or a `*-weekly.md`, whose goal and commitment check supplies the monthly pulse. Take whichever is most recent:

```
Bash: last_full=$(find "$OV/reflections" -name '*-review.md' ! -name '*-review-pulse.md' 2>/dev/null | sort | tail -1)
Bash: last_pulse=$(find "$OV/reflections" \( -name '*-review-pulse.md' -o -name '*-weekly.md' \) 2>/dev/null | sort | tail -1)
```

Note: a no-change pulse intentionally skips writing a file (per Output → Pulse write gate), so `last_pulse` may understate by up to 30 days. That's tolerable: the cost of suggesting one extra pulse during the gap is far lower than the cost of a missed pulse.

Decision rules align with the cadence table above (full = quarterly = 90 days, light pulse = monthly = 30 days). Branch on `last_full` first, then refine with `last_pulse`:

- **No prior full review** → run the **full quarterly form** to establish the baseline. Lookback = 90 days for Context Loading. First-run case.
- **<30 days since last full review** → no review needed; tell user "last full review was N days ago, full quarterly cadence not due yet, no monthly pulse needed yet either". Offer full only if user explicitly asks.
- **30-89 days since last full review** → check `last_pulse`:
  - If **<30 days since last pulse** → no pulse needed yet ("monthly pulse already done N days ago"). Offer full only if user explicitly asks.
  - Otherwise (no pulse or ≥30 days) → **light pulse default**. The full quarterly cadence has not yet rolled over; offer full only if user explicitly asks.
- **90-120 days since last full review (on cadence)** → full quarterly form. The canonical quarterly review.
- **>120 days since last full review (overdue)** → full quarterly form with an honest gap note ("last full review was N days ago, X days past the quarterly cadence").

Stale-goal floor: every full review must explicitly resolve each item under `## Goals requiring an explicit decision` in `directions.md` (retire, redefine, or re-commit) and any goal older than 1 year with no progress evidence. Do not let stale goals roll over silently.

## Prerequisites

The routed context helper below stops toward `/introspect` for a missing
declared profile and reports staleness for selected profiles.

## Context Loading

1. Reuse the current `review` Repomix context artifact from `$hi`; for direct
   invocation, run `uv run scripts/context_bundle.py --intent review`.

2. Use the artifact's latest session sections as the continuity seed. Search the
   selected lookback window, 90 days for a full review or 30 days for a pulse,
   and triage QMD candidates before reading 3 to 5 relevant source sections. Do
   not preload every reflection in the window.

3. **Pull goal-related updates from the local vault, bounded to the lookback window.** Do NOT issue an unbounded `Grep(path: "$OV/")` — an unbounded grep will pull stale historical matches that skew the review. Use `find -print0 | xargs -0 grep` so recency actually binds. Substitute `<N>` with the lookback (90 for full, 30 for pulse):
   - `Bash: find "$OV"/daily "$OV"/reflections "$OV"/gtd "$OV"/wiki -type f -name "*.md" -mtime -<N> -print0 2>/dev/null | xargs -0 grep -HnE "目标|goal|progress|进展|milestone" 2>/dev/null`: recency-bounded goal and progress mentions across both languages in one pass. Safe with an empty working set (xargs does nothing if stdin is empty).
   - Add today's daily note only if current capture is necessary for a disputed
     or missing goal state.

4. Read key goal-note sections directly. Read a complete file only when the
   full goal record is required. If a referenced note is genuinely missing
   from the local vault, report the gap.

## Analysis

Walk `directions.md` in its current shape: one pass per H3 under `## Mid-term
direction`, then every row of the commitments table (full review) or the rows
due since the last pulse (monthly), then `## Long-term direction` on full
reviews only: does each mid-term heading still ladder into a long-term line,
and does any long-term line have no mid-term carrier? The category names come
from the file at runtime, never from this page.

### Progressing
Which goals have evidence of recent activity? Look for:
- Goal notes edited recently
- Daily notes mentioning goal-related topics
- Reflections that referenced these goals

### Neglected
Which goals appear in directions.md but have NO recent activity? Look for:
- Goals not mentioned in any recent reflection
- Goal notes not edited in 30+ days
- Topics absent from daily notes

### Shifted
Have the user's priorities changed? Look for:
- New topics appearing frequently in recent notes that aren't in directions.md
- Goals that were active 3 months ago but have gone quiet
- Contradictions between stated goals and recent behavior

## Review Output

Present the review interactively, category by category. For each finding, cite the specific source note.

After discussing, present the draft and obtain approval before writing:

**File:** `<paths.reflections>/YYYY-MM-DD-review.md` for **full** reviews; `<paths.reflections>/YYYY-MM-DD-review-pulse.md` for **monthly light pulse** runs. The distinct suffix lets the Cadence Bash check (above) tell them apart so a pulse does not silently defer the next quarterly review.

**Pulse write gate:** no material change means no pulse file. Full reviews
still produce a draft when goals are unchanged, so an approved save can anchor
the quarterly cadence. A declined save must not be reported as a completed
cadence anchor. Neither branch changes `directions.md` without separate approval.

Keep a short summary, progressing/neglected/shifted findings under the actual
direction headings, commitment evidence and state, and source links. Full reviews
also show the long-term ladder and explicit resolutions of stale goals. Include
new interests, experiments, or a trend comparison only when supported; do not
fill fixed category templates or manufacture a quota of actions.

## Session Log

Run `uv run scripts/session_log.py --type review --duration <minutes>` and fill
the compact Continuity, Anomalies, and Operations sections per
`protocols/session-log.md`. Record the next review trigger and actual save/update
outcomes, including a no-change pulse or declined write. Logs never block the session.

## Wrap Up

The approved review file is the durable output. Daily notes remain read-only.
Report the actual saved location, no-change pulse, or declined write; never
announce a save that did not happen.

Suggest follow-ups:
- `/hi` for daily check-ins between reviews
- `/weekly` for lighter weekly check-ins
- `/decision` if any goal needs a decision about continuing or stopping

## Trend Analysis (if prior reviews exist)

If previous review files exist in `<paths.reflections>/`, compare:
- Which goals were "Neglected" last time — are they still neglected? (Chronic neglect signal)
- Which goals were "Progressing" — have they continued? (Momentum signal)
- Which "Suggested Experiments" from last review were actually done? (Follow-through signal)

Present trend as:
| Goal | Last Review | This Review | Trend |
|------|------------|-------------|-------|
| [goal] | Progressing | Progressing | Sustained momentum |
| [goal] | Neglected | Neglected | Chronic neglect — needs decision |
| [goal] | Progressing | Neglected | Lost momentum — investigate |
