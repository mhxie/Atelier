---
description: Goal review for quarterly full reviews and monthly light pulses.
---
# Goal Review

> Also reachable via `/hi <natural language>` (e.g., `/hi review my goals`,
> `/hi goal pulse`, `/hi check directions`). See `harness/intents.toml`
> `[intents.review]` for the full pattern list. Both paths execute this same procedure.

Review progress on near/mid/long-term goals. Surface what's progressing, what's neglected, and what has shifted.

## Cadence

| Type | Frequency | What |
|---|---|---|
| **Full review** | Quarterly (start of Q1 / Q2 / Q3 / Q4, i.e., early Jan / Apr / Jul / Oct) | Walk every goal in `directions.md`, decide keep / shift / drop. Retire >1y stale goals. Trend table vs. prior reviews. |
| **Light pulse** | Monthly | 5-min check inside `/weekly` or standalone: any goal materially advanced? Any neglected? Any newly born? No file write required if nothing surfaced. |
| **Annual reset** | Yearly (Jan or birthday) | Full rebuild anchored to `<year>小目标`. Touches `directions.md` directly. Often paired with `/introspect`. |

Inspect the last full review and the last pulse-equivalent so the monthly cadence does not produce duplicate pulses within 30 days. A pulse-equivalent is either a standalone `*-review-pulse.md` OR a `*-weekly.md` (because `/weekly`'s Honest Assessment section doubles as the monthly pulse — see Cadence table). Take whichever is most recent:

```
Bash: last_full=$(find "$OV/reflections" -name '*-review.md' ! -name '*-review-pulse.md' 2>/dev/null | sort | tail -1)
Bash: last_pulse=$(find "$OV/reflections" \( -name '*-review-pulse.md' -o -name '*-weekly.md' \) 2>/dev/null | sort | tail -1)
```

Note: a no-change pulse intentionally skips writing a file (per Output → Pulse write gate), so `last_pulse` may understate by up to 30 days. That's tolerable — the cost of suggesting one extra pulse during the gap is far lower than the cost of a missed pulse.

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

1. Check if `profile/identity.md` exists. If not, tell the user: "No profile found. Run `/introspect` first to build your self-model." and stop.
2. Read only its `Last built:` line. If older than 7 days, warn: "Your profile is stale (built on [date]). Consider running `/introspect` to refresh."

## Context Loading

1. Reuse the current `review` context projection from `$hi`; for direct
   invocation, run `uv run scripts/context_bundle.py --intent review
   --byte-budget 20480 --format json`.

2. Use the projected reflection headings and closing sections as the continuity
   seed. Search the selected lookback window, 90 days for a full review or 30
   days for a pulse, and triage bounded capsules before reading 3 to 5 relevant
   source sections. Do not preload every reflection in the window.

3. **Pull goal-related updates from the local vault, bounded to the lookback window.** Do NOT issue an unbounded `Grep(path: "$OV/")` — an unbounded grep will pull stale historical matches that skew the review. Use `find -print0 | xargs -0 grep` so recency actually binds. Substitute `<N>` with the lookback (90 for full, 30 for pulse):
   - `Bash: find "$OV"/daily-notes "$OV"/reflections "$OV"/gtd "$OV"/wiki -type f -name "*.md" -mtime -<N> -print0 2>/dev/null | xargs -0 grep -HnE "目标|goal|progress|进展|milestone" 2>/dev/null` — recency-bounded goal and progress mentions across both languages in one pass. Safe with an empty working set (xargs does nothing if stdin is empty).
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

After discussing, write a review file:

**File:** `<paths.reflections>/YYYY-MM-DD-review.md` for **full** reviews; `<paths.reflections>/YYYY-MM-DD-review-pulse.md` for **monthly light pulse** runs. The distinct suffix lets the Cadence Bash check (above) tell them apart so a pulse does not silently defer the next quarterly review.

**Pulse write gate**: skip the pulse file write entirely if the pulse surfaced no material change (no goal advanced, none neglected newly, none newly born). Tell the user "no material change this month" and exit without a file. Empty pulse artifacts pollute the longitudinal record and confuse the next cadence check. Full reviews always write a file (even if findings are mostly "still on track") because the quarterly cadence anchor matters.
```markdown
# Goal Review — YYYY-MM-DD

## Summary
[One paragraph: overall assessment of goal progress since last review]

## By Category

### Mastery and impact
- **Progressing:** [goals with evidence] — Source: [[Note Title]]
- **Neglected:** [goals with no recent activity] — Source: [[Note Title]]
- **Shifted:** [any changes in direction]

### Learning
- **Progressing:** ...
- **Neglected:** ...
- **Shifted:** ...

### Energy and family
- **Progressing:** ...
- **Neglected:** ...
- **Shifted:** ...

### Capacity and optionality
- **Progressing:** ...
- **Neglected:** ...
- **Shifted:** ...

## Commitments
| Commitment | Observable next evidence | State | Source |
|---|---|---|---|

## Long-term ladder (full review only)
- [long-term line] ← [mid-term carrier or `(none)`]

## Emerging Interests
[Topics appearing in recent notes that aren't captured in any goal — potential new directions]

## Suggested Experiments
[2-3 concrete actions for the next review period, tied to specific neglected goals or emerging interests]

## Notes Referenced
[List of all notes cited during this review]
```

## Session Log

After writing the review file, emit a session log:
1. `Bash: uv run scripts/session_log.py --type review --duration <minutes>`
2. `Edit` the created file to populate sections from session data (agents dispatched, searches, questions, frameworks, anomalies). The canonical fill-in guide lives in `protocols/session-log.md` § "Section Guidance". Leave empty sections with headers only. If the write fails, warn and continue.

## Wrap Up

The review file in `<paths.reflections>/` is the durable session output. Daily notes are user-authored; the system reads them but does not modify them. Tell the user the review has been saved and where to find it.

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
