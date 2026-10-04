## Capture intent

Outcome: record user-authored factual material without turning it into a
reflection or silently choosing an ambiguous destination.

Select one Scribe operation from the content shape:

| Content shape | Operation | Target |
|---|---|---|
| Date-stamped narrative | `daily_note` | `<paths.daily_notes>/` |
| Trip-associated restaurant + score / 必点 | delegate to `/dine` Intent C (`protocols/dining-capture.md`) | meal log and confirmed trip note |
| Other restaurant + score / 必点 | `dining_row` | configured meal-history tracker |
| New person with bio context | `people_stub` | `<paths.people>/` |
| Action item with deadline or area, or close-out toggle | `gtd_entry` (`add` / `toggle_done` / `toggle_killed`); adds use `+ [ ]`, due `[[YYYY-MM-DD]]` | add: `<paths.gtd>/YYYYQn.md` for the due date, else today's quarter; toggle: the task's file |
| Other factual capture | `generic` | suitable path under `<paths.wip>/` |

Resolve the exact target from the user's existing private layout. Ask once
when multiple destinations are plausible. Never infer trip association from
city, address, or date alone; the user must name the trip or establish the
current trip explicitly.

### Zero-files recovery (orchestrator side, before dispatch)

- `gtd_entry` — if `<paths.gtd>/` is empty, ask the user once for a default GTD filename and create the file in the dispatch context (or skip the dispatch and surface the question). Do not pass an empty `target_file` to the Scribe.
- `generic` — if no `<paths.wip>/` path is obvious from content, propose `<paths.wip>/<short-slug>.md` and confirm with the user before dispatch.
- `dining_row` / `daily_note` — if the canonical target file or directory does not exist, the Scribe will return a clarification request; route it back to the user to supply the path or filename rather than retrying with a guess.

Use the local effective date: before 03:00, use the previous calendar day.
Daily notes remain user-authored. The Scribe `daily_note` operation is the only
system path allowed to record the user's verbatim daily-note content.

Dispatch Scribe with the raw user text and resolved operation fields. Do not
pre-summarize or add facts.
