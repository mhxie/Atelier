---
description: Digest the mobile-capture submodule into the persistent L2 destinations and stage zettelm deletions.
---
# Sync

> Also reachable via `/hi <natural language>` (e.g., `/hi sync`, `/hi digest zettelm`,
> `/hi process zettelm`, `/hi sync mobile notes`). See `harness/intents.toml`
> `[intents.sync]` for the row's example phrases. Both paths execute this procedure.

Digest the mobile-capture submodule `<paths.zettelm>/` into the persistent L2 destinations. Each markdown file is enriched (light typo fixes, backlink injection) and merged into `<paths.daily_notes>/YYYY/MM/<date>.md`; each attachment is routed to the right `$OV/<domain>/raw/`; a 3-prompt footer is appended to the most recent daily note; the originals in zettelm are staged for deletion for you to commit.

Nothing in `zettelm/` is expected to survive long-term — once `/sync` completes successfully, the submodule is empty (or nearly so) until the next mobile capture arrives.

## Success criterion

The sync is successful when, for every file ingested:

1. The target daily note contains the original narrative verbatim plus injected `[Display](<path>)` backlinks (or, for attachments, a `[[<attachment-name>]]` reference at the right anchor point in the day's narrative).
2. Every attachment has been moved to the appropriate `$OV/<domain>/raw/YYYY/MM/` and is reachable via the backlink in (1).
3. No injected `[Display](<path>)` resolves to a missing target (Reviewer-verified).
4. zettelm no longer holds the file (staged for git deletion; commit is the user's call).

You verify by opening any of today's daily notes and seeing names / places / restaurants as live links, and by checking the attachment landed where the daily note's backlink claims.

## Flow

### 0. Pull the submodule (orchestrator, mandatory first step)

The zettelm submodule has its own remote; mobile captures are pushed there from the phone. The local working copy can be arbitrarily stale. Always pull before scanning so the digest operates on the full inbox, not a stale snapshot.

```bash
Bash: git -C "<paths.zettelm>" pull origin main
```

If the pull fails (network down, auth expired, no remote configured), warn the user and ask whether to proceed with local-only content or abort. Do not silently scan stale local state.

### 1. Scan and classify (orchestrator)

```bash
Bash: ls -la "<paths.zettelm>/"
```

Classify each entry into one of:

| Class | Detection | Default destination |
|---|---|---|
| Dated markdown | filename matches `YYYY-MM-DD.md` (or close variants like `MM-DD-YY`) | `<paths.daily_notes>/<YYYY>/<MM>/<YYYY-MM-DD>.md` |
| Undated markdown | other `.md` files | `<paths.daily_notes>/<YYYY>/<MM>/<YYYY-MM-DD>.md` resolved from today's effective date |
| Attachment | `.pdf`, `.jpg`, `.jpeg`, `.png`, `.heic`, `.m4a`, `.mp3` | inferred `$OV/<domain>/raw/<YYYY-MM-DD>-<slug>.<ext>` (flat, date-prefixed; observe the existing filename convention in the target `<domain>/raw/` before writing) |
| README / dotfile | `README.md`, `.git*` | ignore; never touch |

If zettelm is empty, print "zettelm 已清空 — 没什么要 digest." and stop. Do not dispatch any agents.

### 2. Gather backlink candidates (Researcher, parallel)

For each markdown file, read its content. Across the batch, collect candidate entities (names, restaurants, places, concepts). Dispatch a single **Researcher** with the entity list and these targets to search:

- `<paths.people>/` — for person backlinks (verify via `uv run scripts/people.py "<name>"` rather than raw grep, so DL-tagged file names are resolved correctly)
- `<paths.wiki>/` and any localized shadow wikis from `[paths.wiki_localized]` — for concept / place backlinks
- the user's dining-log file under `<paths.travel>/` — for restaurant rows (existing 餐厅 rows → backlink target)
- `<paths.papers>/`, `<paths.preprints>/` — only if a paper title is mentioned

Researcher returns one of three states per entity, and the orchestrator handles each differently. The rule that drives the split: **display text must match the linked file's title verbatim, or no link is created.** A display/title mismatch (e.g., a sub-brand mentioned in capture pointing at the parent-brand vault note, or an informal employer name pointing at a different filename) is the failure mode — readers can't trust links if the visible text disagrees with the destination.

| State | Trigger | Treatment in daily note |
|---|---|---|
| Clean match | Entity text equals (or trivially equals modulo whitespace/case) the title of an existing note in `people/`, `wiki/`, `travel/`, etc. | Wrap as `[Title](<../../../<tier>/<Title>.md>)`. Display = file title verbatim. |
| Alias / partial match | An existing file is conceptually related but the title differs (e.g., a sub-brand vs. parent-brand filename, an informal name vs. canonical title, a generic noun vs. a year-scoped index) | Leave the original text plain. Do NOT create a link with mismatched display text. Surface the entity in the end-of-sync entity-curation prompt (step 9) so the user can decide whether to create a properly-named note. |
| NEW | No existing file at all (e.g., new person, new restaurant, new venue) | Leave plain. Surface in entity-curation prompt. For obvious person mentions with bio context, queue a `people_stub` Scribe op. |

Wiki-tier targets under `<paths.wiki>/` are the only exception to the GitHub-link convention — those are written as Obsidian `[[Title]]` for the trust engine. Same display=title rule applies: do not use `[[Title|Alias]]` pipe-aliases (the trust parser doesn't follow them, and the display/title divergence is the same anti-pattern).

### 3. Draft enriched merges (Curator, snapshot-first)

For each markdown file:

1. **Snapshot the source**: `cp "<paths.zettelm>/<file>.md" "<paths.cache>/sync-<basename>.md"` so the Curator works from a stable snapshot per the snapshot-first protocol in `protocols/orchestrator-actions.md`.
2. **Dispatch Curator** with: the snapshot path, the Researcher's entity → link map, the target daily-note path, and these enrichment rules:
   - Typo correction: light hand only. Fix obvious typos in Chinese (漏字 / 错字 / 拼音错位) and English (autocomplete glitches). Do not rewrite voice, restructure sentences, or "improve" the text. If a phrase reads awkwardly but is unambiguous, leave it.
   - Backlinks: wrap exactly the entities returned in the link map. Do not invent new backlinks. Do not link generic words (`今天`, `早上`, `吃了`).
   - Preserve original line breaks and paragraph structure.
   - Curator returns a **proposed merge block** (verbatim original + light fixes + backlinks), not a full file rewrite. The orchestrator appends to the target daily note.

Dispatch in parallel across markdown files (independent targets → safe to parallelize).

### 3b. Detect dining captures missing names or context

Scan the digested batch for **descriptive-only restaurant captures** — narrative text that names a meal kind, cuisine, or descriptor (e.g. a barbecue-style noun, a cuisine adjective, or a "neighborhood + 大排档" placeholder) without naming a specific restaurant or dish detail sufficient for a dining-log row. For each such capture:

1. Check whether zettelm contains an associated attachment (bill photo, receipt, menu photo) on or near the same date. Match by mtime proximity (±24h) and by visual-content heuristic on the filename.
2. **If an attachment is present**: assume the user already provided augmentation. Stage the attachment for `<domain>/raw/` routing (step 4) and queue a dining-log row that references it. No prompt needed.
3. **If no attachment is present**: do NOT auto-create a dining-log row from the descriptive text alone. Add a one-line reminder to the final summary (step 9): "`<date>`: `<descriptive phrase>` was captured without a restaurant name. Next time, snap the bill / take a photo of the menu / dictate the restaurant name into zettelm, or clarify now."
4. Wait for clarification only if the user opts to provide it in-conversation; otherwise skip the dining-log row and continue. The narrative text still merges into the daily note as plain (un-backlinked) text.

This pattern applies symmetrically to other capture types whose downstream entry requires a name or identifier (e.g., a new doctor visit captured as "today saw the dentist" without the clinic name). Generalize as: if a capture lacks the identity fields its downstream tier requires, defer the structured entry and surface a reminder.

### 4. Route attachments (orchestrator, per-attachment confirm)

For each attachment:

1. Infer destination from filename + adjacent markdown narrative. Build the target path as `$OV/<domain>/raw/<YYYY-MM-DD>-<slug>.<ext>`:
   - `<domain>` from narrative context (health, travel, finance, abroad, etc.).
   - `<YYYY-MM-DD>` from the date in an adjacent markdown filename (preferred) or the file's mtime (fallback).
   - `<slug>` is a kebab-case descriptor derived from the original filename and surrounding narrative (e.g., a generic `Scanned Document 12.pdf` referenced near a lease-renewal narrative → `lease-renewal-scan`).
   - `<ext>` is the original extension, lower-cased.
2. Before proposing the target, list the existing files in `$OV/<domain>/raw/` to confirm the actual filename convention in that domain — some domains may differ. Match what's there.
3. Confirm once with the user: "`<filename>` → `<full target path>`. OK?"
4. On approval: `mv` the file. Stage a backlink fragment for the corresponding daily note: `\n\n附件: [<short descriptor>](<../../../<domain>/raw/<final-filename>>)` using GitHub-style markdown links with angle brackets to handle spaces (or a more contextual placement if the Curator's proposed block already references it).
5. On reject: ask for an alternate domain or slug, or skip (leave in zettelm).

`protocols/raw-indexing.md` documents the cross-cutting clickable-index pass over `<domain>/raw/` content; this command's only contract with that protocol is matching the existing filename convention in the target domain.

### 5. Generate 3 prompts (Challenger, footer)

Once all merge blocks are drafted and reviewed in (3), dispatch a single **Challenger** with:

- The digested batch (the markdown content being merged today).
- The past 3 daily notes (`<paths.daily_notes>/`, sorted by date, exclude today's).
- The user's `profile/identity.md` themes and `profile/directions.md` active goals.

Challenger returns exactly 3 prompts grounded in this material. Mix is allowed: 1-3 may be reflective questions on what was captured; 1-2 may be tangential "random thought" provocations the captures suggest. Each prompt one line, no preamble.

The orchestrator formats them as:

```markdown

## Prompts (YYYY-MM-DD)

1. <prompt 1>
2. <prompt 2>
3. <prompt 3>
```

Append this block to the **most recent daily note** receiving content from this sync (resolved in step 1). One footer block per sync, dated by sync date, not by daily-note date — so footers from successive syncs stack chronologically and remain skimmable.

### 6. Quality gate (Reviewer)

Before any write goes through Scribe, dispatch a single **Reviewer** with all proposed merge blocks + the prompts footer. Reviewer checks:

- Every `[Display](<path>)` in the merge blocks resolves to an existing file (people / wiki / travel / etc.). NEW-entity placeholders are acceptable only if the orchestrator has also queued a corresponding `people_stub` capture.
- Every attachment backlink points at a path that the orchestrator's attachment moves will produce.
- No daily-note content is being silently rewritten (only added).
- The footer block's prompts don't echo content that wasn't in the digested batch + recent daily notes (no hallucinated topics).

Any failure → orchestrator pauses, fixes (re-dispatches Curator with corrections, asks user, etc.), re-runs Reviewer. Do not proceed to write on a failed gate.

### 7. Write (Scribe, parallel)

Scribe `daily_note` accepts `mode: create | append | merge` per `.claude/agents/scribe.md`. The orchestrator selects the right mode per target state. The Curator does NOT edit daily notes (forbidden by `.claude/agents/curator.md`); the Curator's role here ends at producing the enriched merge-block draft in step 3. Composition of the final merged body when an existing daily note has content is the orchestrator's responsibility, not Curator's.

| Target state | Scribe `mode` | Behavior |
|---|---|---|
| Does not exist | `create` | Scribe creates the file. Pass an H1 date header (`# Day, Month Dth, YYYY` matching the existing daily-note style) followed by the step-3 merge block as `raw_content`. |
| Exists but empty / whitespace-only | `create` | Same as does-not-exist. |
| Exists with content | `merge` | Orchestrator composes the merged body (see below), then dispatches Scribe with `existing_content` set to the file's current bytes and `raw_content` set to the new merge block. Scribe performs the verbatim-preserving merge per its `daily_note` contract. |

**Composing the merge body for non-empty targets** (orchestrator-side, before Scribe dispatch):

1. Read the existing target file.
2. Show the user a two-column view: existing content vs. step-3 merge block.
3. Ask where to splice: at the top, at the bottom, before a named heading, after a named heading, or inline at a user-specified anchor. If the existing note has structure (e.g. a `## Plan` block, a `## Notes` block), surface the heading list so the choice is concrete.
4. Compose the spliced body inline; do NOT dispatch the Curator (Curator-on-daily-notes is a contract violation). Show the unified diff.
5. User confirms the diff.
6. Dispatch Scribe with `daily_note` operation, `mode: merge`, `target_file: <path>`, `existing_content: <pre-merge bytes>`, `raw_content: <new merge block>`. Scribe handles the verbatim-preserving splice per its own contract.

**Override**: `/sync --append` switches to `mode: append`; the orchestrator skips the splice prompt and Scribe appends the merge block at end-of-file. Use when you intentionally want a fast bulk path and don't care about splice placement.

For the most recent daily-note target only, also include the `## Prompts (YYYY-MM-DD)` footer block at the end of the merged body. The footer is part of the merge content, not a separate write.

Dispatch Scribe calls in parallel across distinct target files. Queue any NEW-entity `people_stub` operations (from step 2) in the same parallel batch.

### 8. Clean up (orchestrator, stage-only by default; user commits)

After all Scribes return successfully, stage the deletions inside zettelm and stop. The user runs the commits explicitly — this matches the success criterion (`zettelm no longer holds the file (staged for git deletion; commit is the user's call)`) and the local-first write contract: nothing mutates git history or a remote without an explicit user action.

```bash
Bash: git -C "<paths.zettelm>" rm <digested files...>
Bash: git -C "<paths.zettelm>" status   # show what's staged
```

Print the suggested follow-up commands verbatim (the user pastes; no auto-execution):

```bash
# Inside the zettelm submodule
cd "<paths.zettelm>"
git commit -m "sync YYYY-MM-DD: digested <N> files"
git push origin HEAD

# Bump the submodule pointer in the parent vault repo
cd "$OV"
git add zettelm
git commit -m "bump zettelm pointer after sync YYYY-MM-DD (N files)"
# git push  # optional; some users keep $OV local-only
```

**Override**: `/sync --auto-commit` opts into full automation — commit + push in zettelm, then commit (no push) in the parent. Use when you've decided zettelm hygiene is mechanical and you don't want the four-step prompt every digest. Default stays stage-only because the local-first contract treats git-history mutation as a deliberate user action, not a side effect of a capture-digest command.

**Failure handling (auto-commit mode)**: if the push fails (network, auth, conflict), the local commits still happen so the deletions persist. The user is prompted to push manually later. Sync does NOT roll back successful local commits to retry pushes.

### 8b. Entity-curation prompt (orchestrator)

After cleanup, before the final summary, list every entity from step 2 that landed in the "Alias / partial match" or "NEW" bucket (i.e., everything that did NOT get linked in the daily notes). Group by category for readability:

```
Detected entities without matching notes — create any?

Venues:   <venue-1>, <venue-2>, ...
Orgs:     <org-1>, <org-2>, ...
People:   <person-1>, <person-2>, ...
Places:   <place-1>, ...
Other:    <other-1>, ...
```

User responds with the names they want notes for. For each accepted:

- Person → dispatch Scribe `people_stub` with the user-provided full name and one-line bio from the daily-note context.
- Venue / restaurant / hotel → ask the user for the canonical filename and the right tier (`travel/`, `people/`, etc.); orchestrator creates a stub.
- Concept / org → propose a wiki entry under `<paths.wiki>/` or a draft under `<paths.wip>/`; let the user pick.

After the stubs land, dispatch a follow-up Researcher pass over today's freshly written daily notes to upgrade plain-text mentions to GitHub-style links. Same display=title rule applies. Re-write the affected daily notes only if the user approves the diff.

If the user declines all, skip stub creation and move on. The plain-text mentions stay; the next `/sync` run will surface the prompt again if zettelm captures the entity again.

### 9. Summary

Print a one-screen summary:

| Item | Count / Detail |
|---|---|
| Markdown digested | N → `daily-notes/...` |
| Attachments routed | N → `<domain>/raw/...` |
| Backlinks injected | N (resolved) + N (NEW, stubbed) |
| Prompts footer appended to | `daily-notes/<YYYY>/<MM>/<YYYY-MM-DD>.md` |
| zettelm cleaned | N files removed, committed, pushed; submodule pointer bumped in parent |
| Skipped | N (with reasons) |
| Capture reminders | N (unnamed restaurants / clinics / etc. — bring photos or verbal context next time) |

End the session here. The session-start cue will not surface again in this
`/hi` invocation; if the user runs `/hi` later and `zettelm/` has new content,
it cues again.

## Defaults you can override per invocation

| Decision | Default | How to flip |
|---|---|---|
| Capture depth | Daily-note text + auto-propose secondary captures (dining row, GTD entry, people stub) with per-item confirm | Pass `/sync text-only` to skip secondary capture proposals |
| Cleanup | Stage-only: `git rm` inside zettelm; print suggested commit + push commands for the user to run | Pass `/sync --auto-commit` to run zettelm commit + push and parent submodule bump automatically |
| Attachments | Inferred domain + per-attachment confirm | Pass `/sync attachments-stage` to stop after step 4 with attachments dumped in `<paths.cache>/zettelm-attachments/` for manual re-filing later |

## Edge cases

- **Conflict with existing daily-note content.** Use the proper-merge protocol in step 7: re-dispatch Curator with the existing content + the new merge block; user picks the splice point; user confirms diff before write. Never silently overwrite or stamp a "from zettelm" marker.
- **Same date appears in multiple zettelm files** (rare — e.g., two `2026-05-08.md` if user wrote on two mobile devices). Concatenate them in mtime order into a single merge block before the Curator dispatch; the daily note ends up with one continuous narrative, not two stamped sections.
- **Daily note for the target date doesn't exist.** Scribe creates it (this is the documented `daily_note` capture path; the user is still the author, the scribe is the typewriter).
- **Researcher returns nothing for an entity.** Leave it as plain text — never invent a link target. Re-run `/sync` later after a `people_stub` lands and the next backlink scan will catch it.
- **Audio / video attachments.** No transcription in this command. Move to `<domain>/raw/` and inject a backlink; transcript work is a separate command if/when it exists.
- **Empty zettelm.** Step 1 short-circuits; no agent dispatch.
