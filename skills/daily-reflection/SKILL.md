---
name: daily-reflection
description: Daily reflection with on-demand energy and open-exploration branches, grounded in notes and goals.
---
# Reflection

Help the user understand a day, an energy pattern, or an open thread. Select
one branch; do not run all three or turn a focused conversation into a checklist.

## Select the branch

Use the intent selected by `/hi`; a direct `/daily-reflection` defaults to
`reflection` unless the user explicitly requests one of the topical branches.

| Intent | Focus | Result suffix and session-log type |
|---|---|---|
| `reflection` | A named day; recent goals and open actions | `reflection` |
| `energy-audit` | Physical, mental, emotional, and social energy over 14 days | `energy-audit` |
| `explore` | Forgotten threads, cross-domain connections, changes in thinking | `exploration` |

Energy and exploration are available through `/hi energy audit` and
`/hi explore`, or the same choices in its menu. Keep their distinct intent
names when loading context; sharing this procedure does not make them daily reviews.

## Context

1. Determine the effective date: before 03:00 local, use yesterday. Reuse the
   selected intent's current Repomix artifact; otherwise run
   `uv run scripts/context_bundle.py --intent <intent> --effective-date YYYY-MM-DD`.
   Only daily reflection adds its named daily note with
   `--source daily-notes/YYYY/MM/YYYY-MM-DD.md`. Follow the helper's missing-profile
   refusal and selected-profile staleness warnings; do not preload other profiles.
2. Use the packed continuity and bounded QMD candidates, then read the source
   sections needed for the chosen topic. Missing evidence stays unknown.
   `protocols/session-continuity.md` owns continuity; load
   `protocols/epistemic-hygiene.md` for the write-first nudge and attribution rules.
3. For daily reflection, or an explicit TODO update in either topical branch,
   run `uv run scripts/todos.py list --json`. Hold source paths, line numbers,
   and text for matching and guarded closure. An empty queue is a no-op;
   unavailable or malformed output is an anomaly, not a reason to block discussion.

## Daily reflection

Run `uv run scripts/todos.py digest` for prior Next Actions, possible closures,
and stale items. Surface at most one relevant item per category, not the list:

- Ask about the prior action only if its session was a different day.
- Confirm a possible completion before recording it. Missed actions are data,
  not failures; absence of a mention is not completion or abandonment.
- For a stale item, offer keep, kill, or promotion with a real deadline and area.
  Accumulate confirmed changes for Capture below; do not write mid-conversation.

Ground the opening in today's note, a previous thread, or a relevant goal.
Ask a few questions one at a time: first clarify the user's concern, then
examine an assumption or goal, then open a useful alternative. Cite the actual
note behind a callback. Do not require three questions when one has answered the need.

Look for one forgotten connection using a conversation concept and an ISO date
at least three months before the effective day:
`uv run scripts/semantic.py query "<concept>" --before YYYY-MM-DD --top 10 --format json`.
Reframe if thin, read a promising source, and offer the connection as a question.
An empty search is not a license to invent a connection. If a clear pattern would
benefit from a framework, optionally dispatch Thinker; do not make it a routine step.

During discussion, make at most one additional high-confidence callback to a
matching open TODO. Do not resurface it after the user declines.

Close by resurfacing a relevant existing action before creating a new one.
If none fits and fewer than five P0/P1 items are active, suggest one concrete
next action. At five or more, help choose from the queue instead of adding work.
Use a bullet under `## Next Action`; prose there is invisible to `todos.py`.

## Energy branch

Search the last 14 days with
`uv run scripts/semantic.py query "physical mental emotional social energy patterns" --path daily-notes --after YYYY-MM-DD --top 10 --format json`.
Read 3 to 5 relevant source sections, or fewer when evidence is sparse; use
targeted follow-up searches for an unresolved pattern rather than reading every day.

Assess four dimensions from reported evidence:

- Physical: sleep, movement, nutrition, physical complaints.
- Mental: focus, context-switching, learning, creative versus routine work.
- Emotional: stress, joy, conflicts, energizing or draining interactions.
- Social: connection, community, isolation, and the user's social battery.

Summarize each as sources, drains, and net trend, with unknowns explicit.
Ask which depleted dimension matters now, what drain could change, and what
source deserves protection. Tie any adjustment to the user's constraints;
do not infer health facts or force a decision. Retain this four-dimensional
view in the result rather than substituting a generic daily reflection.

## Exploration branch

Start semantically, not with synonym grep. Try a few distinct routes: a recent
theme, an underexplored tag, a related note from 6 to 12 months ago, and a
cross-domain connection. Use bounded `semantic.py query` results; exact tags,
dates, and paths can narrow a named thread after discovery.

Read the selected sources and offer up to four grounded sparks: a forgotten
thread, an unexpected connection, a change in belief, or an evidence gap.
Not writing about something does not establish avoidance. Let the user choose
the thread, retrieve more only for that thread, and ask deepening questions.
Use a framework only when it helps. Preserve useful open questions without
forcing an action or inventing surprise.

## Relevant life context

Relationship, food, dining, and interest check-ins run only when the user brings
them up or current evidence makes them relevant to the chosen topic. Do not
ask every category, backfill unrelated life areas, or generate empty tracking rows.

- **Relationships:** ask one useful question and note observed interactions,
  support exchanged, or a concentration/drain pattern. Distinguish missing data
  from isolation; use known relationship classifications, never guessed ones.
- **Nutrition:** when relevant, use `profile/diet.md` → Daily recap nutrition
  review. If essential meal details are missing, ask once; never infer meals.
  Give the brief verdict, observed flags, known rolling 7-day high-load count,
  and one practical adjustment. Apply that profile's exclusions when counting
  high-load gatherings. Never recommend fasting, skipped meals, or punitive
  exercise as compensation.
- **Dining:** use `profile/diet.md` → Capture tiers, Full health-flag taxonomy,
  and Catalog files. Preserve required fields and the actual visit date; compute
  per-person cost only from sourced party size and total. Never guess amounts.
  An explicitly trip-associated meal follows `/dine` Intent C and
  `protocols/dining-capture.md`, including its confirmation gate, not a Scribe row.
  For an ordinary visit, queue `dining_row` with the sourced fields and raw remarks;
  Scribe reads the target schema. Offer relevant rotation or benefit-tracker updates
  separately; recording a visit does not authorize those additional writes.
- **Interests:** for consumption, a changed interest, or an upcoming event the
  user discusses, follow `protocols/interest-discovery.md`. Record only supported
  events; silence is not a decline. Do not run an interest questionnaire or its
  ingestion solely because a reflection happened.

## Capture

Before saving the result, finish any authorized raw capture. Use
`protocols/intent-capture.md` and the selected operation in
`agents/scribe.md`; do not turn analytical questions or feelings into
daily-note facts. Skip content already recorded. Dispatch independent target
files in parallel; do not substitute a parent write for a refused Scribe operation.

For a confirmed TODO change, pass its exact source, `line_no`, `expected_text`,
and marker glyphs from `todos.py`'s `STATE_MAP` (done `[x]`, killed `[~]`):

| Source | Done | Killed |
|---|---|---|
| GTD | `gtd_entry`, `toggle_done` | `gtd_entry`, `toggle_killed` |
| Reflection | `gtd_entry`, `prefix_line`, `DONE <effective-date>: ` | `gtd_entry`, `prefix_line`, `KILLED <effective-date>: ` |

Scribe re-reads the line and refuses drift. For promotion, obtain a due date
and area, resolve the most recently modified Markdown file directly under
`<paths.gtd>/`, then queue `gtd_entry` with `add`; ask if no target exists.
Record refusals or skipped closures in the compact log; never silently retry
against another line or treat a failed capture as completed.

Dictated daily narratives, ordinary dining rows, verified missing people stubs,
and explicitly requested generic captures retain their typed Scribe routes.
Check `scripts/people.py "<name>"` before proposing a missing-person stub.
Trip-associated dining is handled by Intent C, not pending Scribe work.
Resolve ambiguous targets with the user before writing.

## Result and continuity

Draft `<paths.reflections>/YYYY-MM-DD-<result suffix>.md` for the selected branch.
Keep the useful evidence, insights, source links, and an agreed next action or
open question. Add the energy matrix or exploration sparks when that is the
branch; add life-context findings only when discussed. Do not require empty
sections, engagement scores, or a second copy of captured tracker rows.

Present the draft and obtain approval before saving it. Daily notes remain
user-authored and read-only except authorized verbatim Scribe capture.
Report the actual save/capture outcome and its location.

Use `uv run scripts/session_log.py --type <session-log type> --duration <minutes>`
and fill the compact format in `protocols/session-log.md`: Continuity, Anomalies,
and Operations. Keep the continuation seed even if the result note was not saved;
record that refusal as an operation outcome, not as an approved note.
Logs do not contain a substitute reflection or full user narrative. A logging
failure is disclosed but never blocks the conversation.
